"""Tranche `rosetta-qualite-doc` : un pod qui ne lit que le dépôt sait ce qu'est Rosetta (B-1 à B-4, AD-2, AD-3).

Écrit depuis le critère de sortie de la fiche, sans lire le diff. Les attendus sont des
littéraux (liste des addons, des fichiers d'état, des commandes) ; le snapshot
`tests/surface.json` est la source que le critère nomme pour les variables et les outils.
"""
import importlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()
ARCH = (ROOT / "ARCHITECTURE.md").read_text()
ADDONS_MD = (ROOT / "docs" / "addons.md").read_text()
DONNEES = (ROOT / "docs" / "donnees.md").read_text()
WORKFLOW = (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text()
SURFACE = json.loads((ROOT / "tests" / "surface.json").read_text())

ADDONS = ["courrier", "food", "git", "github", "google", "maps", "marees", "meteo",
          "postier", "quotas", "trace", "transit", "verbatim", "withings"]
STATEFUL = {
    "github": ("ROSETTA_GITHUB_DATA", "/data/github"),
    "google": ("ROSETTA_GOOGLE_DATA", "/data/google"),
    "quotas": ("ROSETTA_QUOTAS_DATA", "/data/quotas"),
    "withings": ("ROSETTA_WITHINGS_DATA", "/data/withings"),
}


def _section(md: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", md, re.S | re.M)
    assert m, f"section « {heading} » absente"
    return m.group(1)


def _readme_config_vars() -> set[str]:
    cfg = _section(README, "Configuration")
    return set(re.findall(r"^\| `([A-Z][A-Z0-9_]+)` \|", cfg, re.M))


# ---------- B-2 : le README est vrai ----------

def test_modules_addons_du_depot_sont_ceux_attendus():
    """Garde-fou du test : la liste littérale reflète src/rosetta/addons."""
    found = sorted(p.stem for p in (ROOT / "src" / "rosetta" / "addons").glob("*.py") if not p.name.startswith("_"))
    assert found == ADDONS


@pytest.mark.parametrize("addon", ADDONS)
def test_readme_liste_chaque_addon_avec_lien_vers_sa_section(addon):
    assert f"| `{addon}` |" in README
    assert f"(docs/addons.md#{addon})" in README


def test_readme_ne_liste_aucun_addon_inexistant():
    rows = re.findall(r"^\| `([a-z_]+)` \| .*\(docs/addons\.md#", README, re.M)
    assert sorted(rows) == ADDONS


def test_readme_tableau_de_configuration_couvre_chaque_variable_lue_par_le_code():
    pinned = {v for vs in SURFACE["env"].values() for v in vs}
    assert len(pinned) == 48  # garde-fou : le snapshot n'est pas vide
    assert pinned - _readme_config_vars() == set()


def test_readme_ne_decrit_pas_de_variable_que_le_code_ne_lit_pas():
    pinned = {v for vs in SURFACE["env"].values() for v in vs}
    assert _readme_config_vars() - pinned == set()


def test_readme_defauts_des_variables_a_defaut_litteral():
    cfg = _section(README, "Configuration")
    for var, default in [("ROSETTA_AUTH", "oidc"), ("ROSETTA_GOOGLE_DATA", "/data/google"),
                         ("ROSETTA_GITHUB_DATA", "/data/github"), ("ROSETTA_WITHINGS_DATA", "/data/withings"),
                         ("ROSETTA_QUOTAS_DATA", "/data/quotas"), ("GITHUB_OWNER", "AntorFr"),
                         ("POSTIER_MAX_PER_HOUR", "10"), ("MAIL_VAULT_ROLE", "rosetta-mail"),
                         ("TZ", "Europe/Paris")]:
        row = next(line for line in cfg.splitlines() if line.startswith(f"| `{var}` |"))
        assert f"`{default}`" in row, row


def test_readme_ne_dit_plus_no_volume():
    assert "no volume" not in README.lower()


def test_readme_dit_que_quatre_addons_persistent_sous_data():
    for _, path in STATEFUL.values():
        assert path in README


def test_readme_commandes_de_lint_et_de_test_sont_celles_de_la_suite_attestee():
    script = (ROOT / "scripts" / "suite-attestee.sh").read_text()
    assert "ruff check ." in script
    assert "pytest --cov" in script
    assert "ruff check ." in README
    assert "pytest --cov" in README
    assert "pip install -e .[dev]" in README


def test_readme_commande_de_lancement_donne_un_hub_qui_repond_health(monkeypatch):
    assert "ROSETTA_AUTH=off" in README and "uvicorn rosetta.main:app" in README
    monkeypatch.setenv("ROSETTA_AUTH", "off")
    from starlette.testclient import TestClient

    from rosetta.main import create_app
    with TestClient(create_app()) as client:
        body = client.get("/health").json()
    assert sorted(body["addons"]) == ADDONS


# ---------- B-3 : docs/addons.md ----------

@pytest.mark.parametrize("addon", ADDONS)
def test_addons_md_a_une_section_par_addon_avec_ses_outils_exacts(addon):
    section = _section(ADDONS_MD, addon)
    documented = set(re.findall(r"^\| `([a-z_0-9]+)` \|", section, re.M))
    expected = {t["name"] for t in SURFACE["tools"][addon]}
    assert expected, "garde-fou : l'addon a des outils dans le snapshot"
    assert documented == expected


def test_addons_md_sections_dans_l_ordre_alphabetique_sans_intrus():
    assert re.findall(r"^## (\S+)$", ADDONS_MD, re.M) == ADDONS


@pytest.mark.parametrize("addon", ADDONS)
def test_addons_md_mode_d_identite_et_variables_requises_sont_ceux_du_code(addon, monkeypatch):
    module = importlib.import_module(f"rosetta.addons.{addon}")
    section = _section(ADDONS_MD, addon)
    identity = re.search(r"\*\*Identité\*\* : `(\w+)`", section).group(1)
    assert identity == getattr(module, "identity", "machine")
    line = re.search(r"\*\*Variables requises\*\* : (.*)", section).group(1)
    documented = set(re.findall(r"`([A-Z_0-9]+)`", line))
    assert documented == set(getattr(module, "required_env", []))


def test_addons_md_identites_litterales():
    users = {"courrier", "git", "github", "google", "withings"}
    for a in ADDONS:
        expected = "user" if a in users else "machine"
        assert f"**Identité** : `{expected}`" in _section(ADDONS_MD, a)


@pytest.mark.parametrize("addon", sorted(STATEFUL))
def test_addons_md_addon_a_etat_donne_variable_et_chemin(addon):
    var, path = STATEFUL[addon]
    section = _section(ADDONS_MD, addon)
    etat = next(line for line in section.splitlines() if line.startswith("- **État**"))
    assert var in etat and path in etat


@pytest.mark.parametrize("addon", sorted(set(ADDONS) - set(STATEFUL)))
def test_addons_md_addon_sans_etat_ne_pretend_pas_ecrire_sur_disque(addon):
    section = _section(ADDONS_MD, addon)
    etat = next(line for line in section.splitlines() if line.startswith("- **État**"))
    assert "/data/" not in etat


def test_addons_md_variables_citees_par_section_existent_dans_le_code():
    pinned = {v for vs in SURFACE["env"].values() for v in vs}
    cited = set(re.findall(r"`([A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+)`", ADDONS_MD))
    assert cited - pinned == set()


# ---------- B-3 : docs/donnees.md ----------

@pytest.mark.parametrize("addon", sorted(STATEFUL))
def test_donnees_md_decrit_le_format_de_chaque_magasin(addon):
    var, path = STATEFUL[addon]
    heading = re.search(rf"^## .*`{var}` \(`{re.escape(path)}`\)$", DONNEES, re.M)
    assert heading, f"section du magasin {var} absente"
    body = DONNEES[heading.end():].split("\n## ")[0]
    assert "users/<safe>.json" in body
    assert "refresh_token" in body


def test_donnees_md_fichiers_annexes_de_google_et_withings():
    google = DONNEES.split("## Google")[1].split("\n## ")[0]
    assert "client_secret.json" in google and "identity_bridge.json" in google and "state.key" in google
    assert "state.key" in DONNEES.split("## Withings")[1].split("\n## ")[0]


# ---------- B-1 : ARCHITECTURE.md ----------

@pytest.mark.parametrize("module", ["__init__", "main", "auth"])
def test_architecture_cite_chaque_module_de_src_rosetta_hors_addons(module):
    assert f"src/rosetta/{module}.py" in ARCH
    assert (ROOT / "src" / "rosetta" / f"{module}.py").exists()


def test_architecture_modules_de_src_rosetta_sont_ceux_attendus():
    found = sorted(p.stem for p in (ROOT / "src" / "rosetta").glob("*.py"))
    assert found == ["__init__", "auth", "main"]


def test_architecture_cite_le_module_commun_des_addons():
    assert "src/rosetta/addons/_common.py" in ARCH


def test_architecture_a_les_sections_organes_securite_deploiement():
    for h in ("Organs", "Security", "Deployment"):
        assert re.search(rf"^#+ {h}\b", ARCH, re.M), h


def test_architecture_ne_cite_aucun_fichier_source_inexistant():
    for rel in set(re.findall(r"`(src/rosetta/[A-Za-z_/]+\.py)`", ARCH)):
        assert (ROOT / rel).exists(), rel


# ---------- B-4 / AD-2 / AD-3 : retrait de .agent/status.md ----------

def test_agent_status_md_n_existe_plus():
    assert not (ROOT / ".agent" / "status.md").exists()


def test_architecture_porte_ad_2_et_ad_3_une_seule_fois():
    ids = re.findall(r"^### (AD-[\w-]+) — ", ARCH, re.M)
    assert ids.count("AD-2") == 1 and ids.count("AD-3") == 1
    assert len(ids) == len(set(ids))


def test_architecture_decisions_durables_du_journal_sont_des_ad_doc():
    ids = re.findall(r"^### (AD-doc-\d+) — ", ARCH, re.M)
    assert len(ids) >= 5
    assert "## Durable decisions from the logbook" in ARCH


def test_aucun_lien_de_doc_ne_pointe_vers_un_fichier_absent():
    for text in (README, ARCH, ADDONS_MD):
        for rel in re.findall(r"\]\((docs/[a-z]+\.md)(?:#[^)]*)?\)", text):
            assert (ROOT / rel).exists(), rel
