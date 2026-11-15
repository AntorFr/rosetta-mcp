"""Ensemble, feature rosetta-mcp-niveau-local : le dépôt déclare son niveau local et le rejeu d'un test nommé.

Besoin (message « Livre » de Monsieur) : `.sdlc.yaml` déclare `tests.local` (ici la suite entière,
car pas de classe lente, avec un commentaire qui le dit) et `tests.rejouer` au gabarit `{tests}` ;
la suite attestée (`commande:`) est inchangée. Hors : changer les tests eux-mêmes.

Le rejeu est joué pour de vrai, avec la commande telle que .sdlc.yaml la déclare, dans une copie
jetable du dépôt (resultats.xml du dépôt intact).
"""
import shlex
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
RAW = (ROOT / ".sdlc.yaml").read_text()
TESTS = yaml.safe_load(RAW)["tests"]
VENV = ROOT / ".venv-suite"


def test_la_suite_attestee_est_inchangee():
    # Panne visée : la déclaration du niveau local touche la commande attestée.
    assert TESTS["commande"] == "scripts/suite-attestee.sh"
    assert TESTS["rapport"] == "resultats.xml"
    assert TESTS["format"] == "junit"
    assert TESTS["delai"] == "15m"


def test_le_niveau_local_est_declare_et_egal_a_la_suite_faute_de_classe_lente():
    # Panne visée : `local:` absent (les phases rejouent la suite entière sans le dire).
    assert TESTS["local"] == "scripts/suite-attestee.sh"


def test_le_niveau_local_n_est_pas_une_liste_de_fichiers():
    # Exclusion : une règle calculée, jamais des fichiers nommés.
    assert "tests/" not in TESTS["local"]
    assert ".py" not in TESTS["local"]


def test_un_commentaire_dit_pourquoi_local_egale_la_commande():
    # Livre : « avec un commentaire qui le dit ».
    lignes = RAW.splitlines()
    i = next(n for n, l in enumerate(lignes) if l.strip().startswith("local:"))
    assert any(l.strip().startswith("#") for l in lignes[max(0, i - 4):i + 1])


def test_le_rejeu_est_declare_avec_le_gabarit_tests():
    assert TESTS["rejouer"] == "scripts/rejouer.sh {tests}"


def test_les_scripts_declares_existent_et_sont_executables():
    for chemin in (TESTS["local"], TESTS["rejouer"].split()[0]):
        f = ROOT / chemin
        assert f.is_file() and f.stat().st_mode & 0o100, chemin


@pytest.fixture
def copie(tmp_path):
    if not (VENV / "bin" / "pytest").exists():
        pytest.skip("needs .venv-suite (made by suite-attestee.sh)")
    for d in ("scripts", "tests", "src", "docs", ".github"):
        shutil.copytree(ROOT / d, tmp_path / d, ignore=shutil.ignore_patterns("__pycache__", "resultats.xml"))
    for f in ("pyproject.toml", "ARCHITECTURE.md", "README.md", "CLAUDE.md"):
        shutil.copy(ROOT / f, tmp_path)
    shutil.copy(ROOT / ".sdlc.yaml", tmp_path)
    (tmp_path / ".venv-suite").symlink_to(VENV)
    return tmp_path


def rejouer(copie, *noms):
    argv = shlex.split(TESTS["rejouer"].replace("{tests}", " ".join(shlex.quote(n) for n in noms)))
    p = subprocess.run(argv, cwd=copie, capture_output=True, text=True)
    rapport = copie / "resultats.xml"
    cas = [(c.get("classname"), c.get("name")) for c in ET.parse(rapport).getroot().iter("testcase")] if rapport.exists() else []
    return p, cas


def test_rejeu_d_un_vrai_test_du_depot_par_son_nom_junit(copie):
    p, cas = rejouer(copie, "tests.test_identity.test_user_sub_reaches_the_tool")
    assert p.returncode == 0, p.stdout + p.stderr
    assert cas == [("tests.test_identity", "test_user_sub_reaches_the_tool")]


def test_rejeu_par_nom_nu_ne_joue_ni_lint_ni_couverture(copie):
    p, cas = rejouer(copie, "test_user_sub_reaches_the_tool")
    assert p.returncode == 0, p.stdout + p.stderr
    assert len(cas) == 1
    sortie = (p.stdout + p.stderr).lower()
    assert "ruff" not in sortie and "coverage" not in sortie


def test_rejeu_de_deux_tests_de_fichiers_differents(copie):
    p, cas = rejouer(
        copie,
        "tests.test_identity.test_user_sub_reaches_the_tool",
        "tests.test_identity.test_machine_token_rejected_on_user_addon",
    )
    assert p.returncode == 0, p.stdout + p.stderr
    assert sorted(n for _, n in cas) == ["test_machine_token_rejected_on_user_addon", "test_user_sub_reaches_the_tool"]


def test_rejeu_d_un_nom_inconnu_n_est_jamais_un_vert_a_zero_test(copie):
    p, cas = rejouer(copie, "tests.test_identity.test_qui_n_existe_pas")
    assert p.returncode != 0
    assert cas == []


def test_rejeu_sans_nom_echoue(copie):
    argv = shlex.split(TESTS["rejouer"].replace("{tests}", ""))
    p = subprocess.run(argv, cwd=copie, capture_output=True, text=True)
    assert p.returncode != 0
