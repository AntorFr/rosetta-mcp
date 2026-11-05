"""Revérification de `rosetta-qualite-doc` : ARCHITECTURE.md:4 datait le code après nettoyage de v0.29.0 (B-1 : la doc colle au code)."""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARCH = (ROOT / "ARCHITECTURE.md").read_text()


def test_version_du_paquet_est_celle_du_nettoyage():
    """Garde-fou : le littéral attendu ci-dessous est bien la version du code livré."""
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert meta["project"]["version"] == "0.30.0"


def test_architecture_statut_date_le_code_apres_nettoyage_de_v0_30_0():
    status = ARCH.split("\n## ", 1)[0]
    assert "cleanup (v0.30.0)" in status
    assert "v0.29.0" not in status


def test_architecture_ne_dit_pas_que_le_code_actuel_est_v0_29_0():
    """v0.29.0 reste cité comme point de départ (relevé, snapshot), jamais comme l'état après nettoyage."""
    for line in ARCH.splitlines():
        if re.search(r"after the `rosetta-qualite` cleanup|as it is after", line):
            assert "v0.29.0" not in line, line
