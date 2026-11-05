"""`state` OAuth signé (famille « state ») : google et withings, une seule implémentation (AD-nettoyage-2)."""
import base64
import hashlib
import hmac
import os
import re
import stat
import time
from pathlib import Path

import pytest

from rosetta.addons import _common, github, google, withings


def _ancien_sign(sub, key, now=None):
    """Le code d'avant (git show origin/epic/rosetta-qualite:src/rosetta/addons/google.py), recopié."""
    payload = f"{int(now or time.time()) + 600}.{sub}"
    sig = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()


def _ancien_verify(state, key):
    try:
        expiry, sub, sig = base64.urlsafe_b64decode(state.encode()).decode().split(".", 2)
        payload = f"{expiry}.{sub}"
        if not hmac.compare_digest(sig, hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()):
            return None
        if time.time() > int(expiry):
            return None
        return sub
    except Exception:
        return None


KEY = b"k" * 32
SUBS = ["alice", "é-ü", "", "x" * 300]


@pytest.mark.parametrize("sub", SUBS)
def test_ancien_state_accepte_par_le_nouveau_et_inversement(sub):
    assert _common.verify_state(_ancien_sign(sub, KEY), KEY) == sub
    assert _ancien_verify(_common.sign_state(sub, KEY), KEY) == sub


def test_octets_identiques_a_l_ancien():
    assert _common.sign_state("alice", KEY) == _ancien_sign("alice", KEY)


def test_expire_refuse():
    assert _common.verify_state(_ancien_sign("alice", KEY, now=time.time() - 601), KEY) is None
    assert _common.verify_state(_ancien_sign("alice", KEY, now=time.time() - 590), KEY) == "alice"


def test_altere_ou_autre_cle_refuse():
    s = _common.sign_state("alice", KEY)
    raw = base64.urlsafe_b64decode(s).decode()
    forged = base64.urlsafe_b64encode(raw.replace("alice", "bob").encode()).decode()
    assert _common.verify_state(forged, KEY) is None
    assert _common.verify_state(s, b"z" * 32) is None
    assert _common.verify_state(s[:-4], KEY) is None
    for junk in ["", "!!!", "bm9wZQ==", base64.urlsafe_b64encode(b"1.2").decode()]:
        assert _common.verify_state(junk, KEY) is None


def test_expiry_non_numerique_signe_refuse():
    payload = "abc.alice"
    sig = hmac.new(KEY, payload.encode(), hashlib.sha256).hexdigest()
    st = base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()
    assert _common.verify_state(st, KEY) is None


def test_state_key_cree_0600_et_stable(tmp_path):
    d = tmp_path / "neuf" / "sous"
    k = _common.state_key(str(d))
    p = d / "state.key"
    assert len(k) == 32 and p.read_bytes() == k
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert _common.state_key(str(d)) == k


def test_state_key_existante_lue_telle_quelle(tmp_path):
    (tmp_path / "state.key").write_bytes(b"ancienne-cle")
    assert _common.state_key(str(tmp_path)) == b"ancienne-cle"


ENV = {google: "ROSETTA_GOOGLE_DATA", withings: "ROSETTA_WITHINGS_DATA"}


def test_sub_avec_points_se_comporte_comme_avant():
    s = _ancien_sign("a.b.c", KEY)
    assert _common.verify_state(s, KEY) == _ancien_verify(s, KEY)


@pytest.mark.parametrize("mod", [google, withings])
def test_addon_utilise_le_repertoire_courant_et_la_meme_cle(mod, tmp_path, monkeypatch):
    monkeypatch.setenv(ENV[mod], str(tmp_path / "a"))
    s = mod._sign_state("alice")
    key = (tmp_path / "a" / "state.key").read_bytes()
    assert _ancien_verify(s, key) == "alice"
    assert mod._verify_state(_ancien_sign("bob", key)) == "bob"
    assert mod._verify_state(s + "x") is None
    # le répertoire est relu à chaque usage (variable changée après import)
    monkeypatch.setenv(ENV[mod], str(tmp_path / "b"))
    assert mod._verify_state(s) is None


def test_google_et_withings_partagent_la_cle_sous_le_meme_repertoire(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV[google], str(tmp_path))
    monkeypatch.setenv(ENV[withings], str(tmp_path))
    assert withings._verify_state(google._sign_state("alice")) == "alice"


def test_plus_de_def_state_hors_github():
    src = Path(_common.__file__).parent
    trouves = {p.name for p in src.glob("*.py") if re.search(r"def _(sign|verify)_state", p.read_text())}
    assert trouves == {"github.py"}


def test_github_garde_son_state(monkeypatch):
    monkeypatch.setattr(github, "_oauth_client", lambda: None)
    s = github._sign_state("alice")
    assert "|" in base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()
    assert github._verify_state(s) == "alice"
