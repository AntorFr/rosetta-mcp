"""Le filet attrape-t-il ce que la fiche promet ? (rosetta-qualite-filet, B-6, B-9)

Chaque test copie le dépôt dans un dossier jetable, y fait UNE modification de la
surface (jamais dans le vrai arbre), et joue `tests/test_surface.py` sur la copie :
le test de caractérisation doit passer sur la copie intacte et échouer sur chaque
modification que le critère nomme. Attendus en dur : intact -> 0, modifié -> échec.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _run_surface(copy: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(copy / "src")
    env.pop("PYTEST_CURRENT_TEST", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         str(copy / "tests" / "test_surface.py")],
        cwd=copy, env=env, capture_output=True, text=True, timeout=300,
    )


@pytest.fixture
def copy(tmp_path):
    dst = tmp_path / "repo"
    dst.mkdir()
    shutil.copytree(ROOT / "src", dst / "src")
    (dst / "tests").mkdir()
    shutil.copy(ROOT / "tests" / "test_surface.py", dst / "tests")
    shutil.copy(ROOT / "tests" / "surface.json", dst / "tests")
    return dst


def _edit(copy: Path, rel: str, old: str, new: str) -> None:
    path = copy / "src" / "rosetta" / rel
    text = path.read_text(encoding="utf-8")
    assert old in text, f"ancre absente de {rel}: {old!r}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_copie_intacte_passe(copy):
    r = _run_surface(copy)
    assert r.returncode == 0, r.stdout[-2000:]


def test_outil_renomme_fait_echouer(copy):
    _edit(copy, "addons/transit.py", "async def train_departures(", "async def train_departs(")
    assert _run_surface(copy).returncode != 0


def test_schema_change_param_ajoute_fait_echouer(copy):
    _edit(copy, "addons/transit.py", "async def metro_disruptions(station: str | None = None)",
          "async def metro_disruptions(station: str | None = None, extra: int = 0)")
    assert _run_surface(copy).returncode != 0


def test_schema_defaut_change_fait_echouer(copy):
    _edit(copy, "addons/transit.py", "async def train_departures(station: str, count: int = 5)",
          "async def train_departures(station: str, count: int = 6)")
    assert _run_surface(copy).returncode != 0


def test_docstring_outil_change_fait_echouer(copy):
    path = copy / "src/rosetta/addons/transit.py"
    text = path.read_text(encoding="utf-8")
    marker = "async def train_departures(station: str, count: int = 5) -> dict:\n"
    assert marker in text
    # remplace la ligne ouvrant la docstring par un texte différent
    after = text.split(marker, 1)[1]
    first = after.lstrip().split("\n", 1)[0]
    assert first.startswith(('"""', "'''")), first
    path.write_text(text.replace(first, '"""Texte modifie par le test."""' if first.endswith('"""') and len(first) > 6 else '"""Texte modifie par le test.', 1), encoding="utf-8")
    assert _run_surface(copy).returncode != 0


def test_route_hub_retiree_fait_echouer(copy):
    _edit(copy, "main.py", '        Route("/", index),\n', "")
    assert _run_surface(copy).returncode != 0


def test_forme_health_changee_fait_echouer(copy):
    text = (copy / "src/rosetta/main.py").read_text(encoding="utf-8")
    assert '"status"' in text
    _edit(copy, "main.py", '"status"', '"etat"')
    assert _run_surface(copy).returncode != 0


def test_variable_denvironnement_ajoutee_fait_echouer(copy):
    _edit(copy, "addons/transit.py", "import os\n",
          'import os\n_X = os.environ.get("ROSETTA_VARIABLE_NOUVELLE")\n')
    assert _run_surface(copy).returncode != 0


def test_variable_denvironnement_getenv_ajoutee_fait_echouer(copy):
    _edit(copy, "addons/maps.py", "import os\n",
          'import os\n_X = os.getenv("MAPS_VARIABLE_NOUVELLE", "d")\n')
    assert _run_surface(copy).returncode != 0


def test_variable_denvironnement_dynamique_fait_echouer(copy):
    _edit(copy, "addons/maps.py", "import os\n",
          'import os\n_N = "A" + "B"\n_X = os.environ.get(_N)\n')
    assert _run_surface(copy).returncode != 0
