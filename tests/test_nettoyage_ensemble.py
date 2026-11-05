"""Tranche `rosetta-qualite-nettoyage` : l'assainissement tient sa promesse (B-5, B-9, P-3, P-4).

Écrit depuis le critère de sortie de la fiche et AD-nettoyage-2/3, sans lire le diff.
Les attendus sont des littéraux ou des mesures fixées au départ (v0.29.0).
"""
import ast
import base64
import hashlib
import hmac
import json
import os
import stat
import time
from pathlib import Path

import pytest

from rosetta.addons import github, google, quotas, withings

SRC = Path(__file__).resolve().parent.parent / "src" / "rosetta"
ADDONS = SRC / "addons"
ADDON_FILES = sorted(p for p in ADDONS.glob("*.py") if p.name not in ("_common.py", "__init__.py"))


def _tree(path):
    return ast.parse(path.read_text())


def _calls(path):
    """Les appels `a.b(...)` / `f(...)` d'un module, en texte pointé."""
    out = []
    for n in ast.walk(_tree(path)):
        if isinstance(n, ast.Call):
            out.append(ast.unparse(n.func))
    return out


# --------------------------------------------------------------------------
# Familles extraites : plus aucune copie dans un addon
# --------------------------------------------------------------------------

def test_client_http_construit_dans_common_seulement():
    # Casse si un addon refabrique son httpx.AsyncClient.
    for p in ADDON_FILES:
        assert "httpx.AsyncClient" not in _calls(p), p.name
    assert "httpx.AsyncClient" in _calls(ADDONS / "_common.py")


def test_sujet_courant_pas_recopie_dans_github_quotas_withings():
    # Casse si un de ces addons redéfinit sa propre lecture du sujet courant.
    for name in ("github.py", "quotas.py", "withings.py"):
        defs = [n.name for n in ast.walk(_tree(ADDONS / name))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        assert "_current_sub" not in defs and "current_subject" not in defs, name
    assert [n.name for n in ast.walk(_tree(ADDONS / "_common.py"))
            if isinstance(n, ast.FunctionDef)].count("current_subject") == 1


def test_remote_user_pas_recopie_dans_withings():
    # La récupération latin-1 -> utf-8 vit dans _common.
    src = (ADDONS / "withings.py").read_text()
    assert "latin-1" not in src


def test_magasin_par_sujet_pas_recopie_dans_quotas_withings():
    # L'écriture atomique (.tmp, chmod, replace) n'existe plus que dans _common.
    for name in ("quotas.py", "withings.py"):
        calls = _calls(ADDONS / name)
        assert "os.replace" not in calls, name
        assert "os.chmod" not in calls, name
    common = _calls(ADDONS / "_common.py")
    assert "os.replace" in common and "os.chmod" in common


def test_state_signe_pas_recopie_dans_google_withings():
    for name in ("google.py", "withings.py"):
        assert "hmac.new" not in _calls(ADDONS / name), name
        assert "state.key" not in (ADDONS / name).read_text(), name


# --------------------------------------------------------------------------
# Comportement du state signé : octets inchangés (P-3)
# --------------------------------------------------------------------------

KEY = bytes(range(32))


@pytest.fixture
def data_dirs(tmp_path, monkeypatch):
    for var in ("ROSETTA_GOOGLE_DATA", "ROSETTA_WITHINGS_DATA"):
        d = tmp_path / var
        d.mkdir()
        (d / "state.key").write_bytes(KEY)
        monkeypatch.setenv(var, str(d))
    return tmp_path


@pytest.mark.parametrize("mod", [google, withings])
def test_state_google_withings_expiry_sub_sig_digest_complet(data_dirs, mod):
    before = int(time.time())
    state = mod._sign_state("léa")
    raw = base64.urlsafe_b64decode(state.encode()).decode()
    expiry, sub, sig = raw.split(".", 2)
    assert sub == "léa"
    assert before + 600 <= int(expiry) <= int(time.time()) + 600
    attendu = hmac.new(KEY, f"{expiry}.léa".encode(), hashlib.sha256).hexdigest()
    assert sig == attendu
    assert len(sig) == 64
    assert mod._verify_state(state) == "léa"


@pytest.mark.parametrize("mod", [google, withings])
def test_state_ancien_format_litteral_accepte(data_dirs, mod):
    expiry = int(time.time()) + 300
    sig = hmac.new(KEY, f"{expiry}.bob".encode(), hashlib.sha256).hexdigest()
    ancien = base64.urlsafe_b64encode(f"{expiry}.bob.{sig}".encode()).decode()
    assert mod._verify_state(ancien) == "bob"


@pytest.mark.parametrize("mod", [google, withings])
def test_state_expire_ou_falsifie_refuse(data_dirs, mod):
    passe = int(time.time()) - 5
    sig = hmac.new(KEY, f"{passe}.bob".encode(), hashlib.sha256).hexdigest()
    expire = base64.urlsafe_b64encode(f"{passe}.bob.{sig}".encode()).decode()
    assert mod._verify_state(expire) is None
    bon = mod._sign_state("bob")
    raw = base64.urlsafe_b64decode(bon.encode()).decode().replace(".bob.", ".eve.")
    assert mod._verify_state(base64.urlsafe_b64encode(raw.encode()).decode()) is None
    assert mod._verify_state("pas-un-state") is None
    assert mod._verify_state("") is None


@pytest.mark.parametrize("mod", [google, withings])
def test_state_cree_la_cle_en_0600_si_absente(tmp_path, monkeypatch, mod):
    var = {google: "ROSETTA_GOOGLE_DATA", withings: "ROSETTA_WITHINGS_DATA"}[mod]
    monkeypatch.setenv(var, str(tmp_path / "neuf"))
    state = mod._sign_state("zoe")
    cle = tmp_path / "neuf" / "state.key"
    assert len(cle.read_bytes()) == 32
    assert stat.S_IMODE(cle.stat().st_mode) == 0o600
    assert mod._verify_state(state) == "zoe"


# --------------------------------------------------------------------------
# Exclusion : github garde son propre state (AD-nettoyage-1/2)
# --------------------------------------------------------------------------

def test_state_github_garde_son_format_sub_ts_sig_tronque(monkeypatch):
    monkeypatch.setenv("GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "secret-fixe")
    state = github._sign_state("alice")
    assert "=" not in state  # padding retiré
    raw = base64.urlsafe_b64decode(state + "=" * (-len(state) % 4)).decode()
    sub, ts, sig = raw.rsplit("|", 2)
    assert sub == "alice"
    cle = hashlib.sha256(b"secret-fixe").digest()
    assert sig == hmac.new(cle, f"alice|{ts}".encode(), hashlib.sha256).hexdigest()[:32]
    assert len(sig) == 32
    # Un état au format google/withings n'est pas accepté par github.
    ex = int(time.time()) + 300
    s2 = hmac.new(cle, f"{ex}.alice".encode(), hashlib.sha256).hexdigest()
    autre = base64.urlsafe_b64encode(f"{ex}.alice.{s2}".encode()).decode()
    assert github._verify_state(autre) is None


def test_github_et_google_gardent_leur_propre_magasin():
    # Nommés non extraits : leur code reste dans leur module (AD-nettoyage-2).
    assert "def _state_key" in (ADDONS / "github.py").read_text()
    assert "def _bridge" not in (ADDONS / "google.py").read_text()  # le pont vit dans _common
    assert "def _user_file" in (ADDONS / "google.py").read_text()
    assert "def _user_file" in (ADDONS / "github.py").read_text()


# --------------------------------------------------------------------------
# Magasin par sujet : format de fichier existant lu et écrit à l'identique
# --------------------------------------------------------------------------

@pytest.fixture(params=[(quotas, {}), (withings, None)], ids=["quotas", "withings"])
def store(request, tmp_path, monkeypatch):
    monkeypatch.setenv("ROSETTA_QUOTAS_DATA", str(tmp_path))
    monkeypatch.setenv("ROSETTA_WITHINGS_DATA", str(tmp_path))
    return request.param


def test_magasin_lit_un_fichier_existant_et_le_reecrit_octet_pour_octet(store, tmp_path):
    mod, _ = store
    users = tmp_path / "users"
    users.mkdir()
    existant = b'{"refresh_token": "r\\u00e9", "userid": 42, "n": [1, 2.5, null]}'
    (users / "alice.json").write_bytes(existant)
    rec = mod._read_user("alice")
    assert rec == {"refresh_token": "ré", "userid": 42, "n": [1, 2.5, None]}
    mod._write_user("alice", rec)
    assert (users / "alice.json").read_bytes() == existant
    assert stat.S_IMODE(os.stat(users / "alice.json").st_mode) == 0o600
    assert not (users / "alice.json.tmp").exists()


def test_magasin_valeur_vide_propre_a_chaque_addon(store, tmp_path):
    mod, vide = store
    assert mod._read_user("absent") == vide
    (tmp_path / "users").mkdir()
    (tmp_path / "users" / "casse.json").write_text("{pas du json")
    assert mod._read_user("casse") == vide


def test_magasin_cree_le_dossier_et_remplace_sans_residu(store, tmp_path):
    mod, _ = store
    mod._write_user("Léa Dupont", {"v": 1})
    mod._write_user("Léa Dupont", {"v": 2})
    fichiers = sorted(p.name for p in (tmp_path / "users").iterdir())
    assert len(fichiers) == 1 and fichiers[0].endswith(".json")
    assert json.loads((tmp_path / "users" / fichiers[0]).read_text()) == {"v": 2}


# --------------------------------------------------------------------------
# Exceptions avalées : aucune sans journal ni noqa motivé
# --------------------------------------------------------------------------

def _nues(path):
    for n in ast.walk(_tree(path)):
        if isinstance(n, ast.ExceptHandler) and all(isinstance(s, (ast.Pass, ast.Continue)) for s in n.body):
            yield n.lineno


def test_aucune_exception_avalee_sans_noqa_motive():
    for p in sorted(SRC.rglob("*.py")):
        lignes = p.read_text().splitlines()
        for ln in _nues(p):
            zone = " ".join(lignes[ln - 1: ln + 2])
            ok = any(f"noqa: {c}" in zone for c in ("S110", "S112", "SIM105"))
            assert ok, f"{p.name}:{ln} exception avalée sans journal ni noqa"
            marque = next(z for z in zone.split("#") if "noqa" in z)
            assert len(marque.split("—")[-1].strip()) > 10 or len(marque.split(":")[-1].strip().split(" ", 1)[-1]) > 10, f"{p.name}:{ln} noqa sans raison"


def test_contextlib_suppress_ne_cache_rien_de_nouveau():
    # SIM105 : try/except/pass remplacé par suppress() est aussi une exception avalée muette.
    for p in sorted(SRC.rglob("*.py")):
        assert "contextlib.suppress" not in _calls(p) and "suppress" not in _calls(p), p.name


# --------------------------------------------------------------------------
# Fonctions les plus longues (v0.29.0) : plus courtes qu'au départ
# --------------------------------------------------------------------------

LONGUEURS_V029 = [
    ("addons/trace.py", "trace_pois", 108),
    ("addons/marees.py", "marees", 105),
    ("addons/google.py", "mail_attachment", 80),
    ("addons/meteo.py", "wind_forecast", 74),
    ("addons/withings.py", "withings_measures", 71),
    ("main.py", "create_app", 71),
]


@pytest.mark.parametrize("fichier,nom,avant", LONGUEURS_V029, ids=[x[1] for x in LONGUEURS_V029])
def test_fonction_longue_plus_courte_qu_au_depart(fichier, nom, avant):
    trouvees = [n for n in ast.walk(_tree(SRC / fichier))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == nom]
    assert len(trouvees) == 1, f"{nom} introuvable ou dupliquée (signature publique)"
    f = trouvees[0]
    assert f.end_lineno - f.lineno + 1 < avant
