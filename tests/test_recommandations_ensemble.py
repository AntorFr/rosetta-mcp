"""Feature `rosetta-qualite-recommandations` : B-8, P-2 — le fichier que l'opérateur lit pour arbitrer.

Écrit depuis la fiche (critère de sortie) et `docs/parcours.md`, sans lire le diff. Les sujets sont
repérés par un mot de leur titre, pas par un identifiant R-n ; les attendus sont des littéraux.
Le critère « git diff vide sur src/, Dockerfile, .github » est une propriété de branche, pas du
dépôt : ici, on vérifie à la place que chaque recommandation encore `open` n'est pas appliquée.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = ROOT / "docs" / "recommandations.md"

# Les sept sujets de `## Livre`, chacun par un mot que son titre doit porter.
SEPT_SUJETS = {
    "user_agent_contacts": "User-Agent",
    "endpoint_quotas": "quotas",
    "yt_dlp": "yt-dlp",
    "image_digest": "digest",
    "actions_par_tag": "Actions",
    "mypy": "mypy",
    "decoupage_google": "google.py",
}


def _doc() -> str:
    assert DOC_PATH.is_file(), "docs/recommandations.md n'existe pas"
    return DOC_PATH.read_text()


def _entrees() -> list[tuple[str, str]]:
    """(titre, corps) de chaque entrée `## R-n — titre`."""
    parts = re.split(r"^## (R-\d+ — .+)$", _doc(), flags=re.M)
    return [(parts[i], parts[i + 1]) for i in range(1, len(parts), 2)]


def _entree_du_sujet(mot: str) -> tuple[str, str]:
    trouvees = [e for e in _entrees() if mot in e[0]]
    assert len(trouvees) == 1, f"sujet « {mot} » : {len(trouvees)} entrée(s) au lieu d'une"
    return trouvees[0]


def _options(corps: str) -> list[str]:
    bloc = corps.split("**Options.**", 1)[1].split("**Recommendation.**", 1)[0]
    return re.findall(r"^- ([A-Z])\. ", bloc, re.M)


# --- nominal : les sept sujets nommés par la fiche, chacun complet -------------------------------

@pytest.mark.parametrize("nom", SEPT_SUJETS)
def test_sujet_nomme_a_constat_deux_options_et_recommandation(nom):
    titre, corps = _entree_du_sujet(SEPT_SUJETS[nom])
    constat = re.search(r"\*\*Finding\.\*\*\s+(\S.{40,})", corps)
    assert constat, f"{titre} : constat absent ou vide"
    assert len(set(_options(corps))) >= 2, f"{titre} : moins de deux options"
    reco = re.search(r"\*\*Recommendation\.\*\*\s+([A-Z])\b", corps)
    assert reco, f"{titre} : la recommandation ne commence pas par une lettre d'option"


# --- bords ---------------------------------------------------------------------------------------

def test_toute_entree_du_fichier_a_au_moins_deux_options_et_une_recommandation():
    entrees = _entrees()
    assert len(entrees) >= 7
    for titre, corps in entrees:
        assert len(set(_options(corps))) >= 2, titre
        assert "**Recommendation.**" in corps, titre
        assert "**Finding.**" in corps, titre


def test_la_recommandation_choisit_une_option_qui_existe():
    for titre, corps in _entrees():
        lettre = re.search(r"\*\*Recommendation\.\*\*\s+([A-Z])\b", corps)
        assert lettre, titre
        assert lettre.group(1) in _options(corps), titre


def test_aucun_sujet_n_est_traite_deux_fois():
    titres = [t for t, _ in _entrees()]
    assert len(titres) == len(set(titres))
    for mot in SEPT_SUJETS.values():
        assert sum(mot in t for t in titres) == 1, mot


def test_le_fichier_accueille_l_arbitrage_de_l_operateur():
    """P-2 : accepter -> feature future, refuser -> consigné dans le même fichier avec sa raison."""
    doc = _doc()
    assert "`accepted`" in doc and "`declined`" in doc
    assert re.search(r"reason", doc)
    for titre, corps in _entrees():
        assert re.search(r"\*\*Status\.\*\* `(open|accepted|declined)`", corps), titre


# --- échec : un constat doit pointer vers du code qui existe -------------------------------------

def test_les_emplacements_cites_existent_dans_le_depot():
    vus = 0
    for titre, corps in _entrees():
        constat = corps.split("**Why it matters.**")[0]
        for chemin, ligne in re.findall(r"`((?:src|tests|docs|\.github)/[\w./-]+|Dockerfile|pyproject\.toml|README\.md)(?::(\d+))?", constat):
            cible = ROOT / chemin
            if "<" in chemin:
                continue
            assert cible.exists(), f"{titre} : {chemin} n'existe pas"
            if ligne:
                assert 1 <= int(ligne) <= len(cible.read_text().splitlines()), f"{titre} : {chemin}:{ligne} hors fichier"
            vus += 1
    assert vus >= 15


def test_les_chemins_courts_de_addons_cites_existent():
    """Les constats écrivent `github.py:76`, `quotas.py:135` : le module doit exister dans addons/."""
    addons = {p.name: p for p in (ROOT / "src" / "rosetta" / "addons").glob("*.py")}
    for titre, corps in _entrees():
        for nom, ligne in re.findall(r"`(?:[\w/]*/)?([a-z_]+\.py):(\d+)", corps.split("**Why it matters.**")[0]):
            if nom in addons:
                assert int(ligne) <= len(addons[nom].read_text().splitlines()), f"{titre} : {nom}:{ligne}"


# --- exclusions : rien n'est appliqué (tant que l'entrée est `open`) -----------------------------

def _statut(mot: str) -> str:
    _, corps = _entree_du_sujet(mot)
    return re.search(r"\*\*Status\.\*\* `(\w+)`", corps).group(1)


def test_contacts_personnels_toujours_par_defaut_tant_que_r_open():
    if _statut("User-Agent") != "open":
        return  # décidé par l'opérateur : l'état du code n'est plus celui de la recommandation
    assert "contact@antor.fr" in (ROOT / "src/rosetta/addons/food.py").read_text()
    assert "contact@antor.fr" in (ROOT / "src/rosetta/addons/trace.py").read_text()


def test_yt_dlp_reste_un_plancher_non_epingle_tant_que_r_open():
    if _statut("yt-dlp") != "open":
        return  # décidé par l'opérateur : l'état du code n'est plus celui de la recommandation
    assert '"yt-dlp>=2025.1"' in (ROOT / "pyproject.toml").read_text()


def test_image_de_base_sans_digest_tant_que_r_open():
    if _statut("digest") != "open":
        return  # décidé par l'opérateur : l'état du code n'est plus celui de la recommandation
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "@sha256:" not in dockerfile
    assert dockerfile.count("FROM python:3.12-slim") == 2


def test_actions_toujours_par_tag_tant_que_r_open():
    if _statut("Actions") != "open":
        return  # décidé par l'opérateur : l'état du code n'est plus celui de la recommandation
    wf = (ROOT / ".github/workflows/docker-publish.yml").read_text()
    assert not re.search(r"uses: \S+@[0-9a-f]{40}", wf)
    assert len(re.findall(r"uses: \S+@v\d", wf)) == 8


def test_mypy_absent_de_la_configuration_et_de_la_ci_tant_que_r_open():
    if _statut("mypy") != "open":
        return  # décidé par l'opérateur : l'état du code n'est plus celui de la recommandation
    assert "[tool.mypy]" not in (ROOT / "pyproject.toml").read_text()
    assert "mypy" not in (ROOT / ".github/workflows/docker-publish.yml").read_text()


def test_google_reste_un_seul_module_tant_que_r_open():
    if _statut("google.py") != "open":
        return  # décidé par l'opérateur : l'état du code n'est plus celui de la recommandation
    addons = ROOT / "src" / "rosetta" / "addons"
    assert (addons / "google.py").is_file()
    assert not (addons / "google").exists()


def test_endpoint_quotas_toujours_appele_tant_que_r_open():
    if _statut("quotas") != "open":
        return  # décidé par l'opérateur : l'état du code n'est plus celui de la recommandation
    source = (ROOT / "src/rosetta/addons/quotas.py").read_text()
    assert 'API = "https://api.anthropic.com"' in source
    assert 'USAGE_URL = f"{API}/api/oauth/usage"' in source
