"""Revérification : le paragraphe stdio du README est complet et vrai (README.md, section « Addon state »)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()
SANS_MAIN = ["courrier", "git", "github", "postier", "quotas", "verbatim"]
AVEC_MAIN = ["food", "google", "maps", "marees", "meteo", "trace", "transit", "withings"]


def _paragraphe():
    m = re.search(r"^Per-addon state is reported.*?(?:\n\n|\Z)", README, re.M | re.S)
    assert m
    return m.group(0)


def test_paragraphe_stdio_nomme_la_commande_et_les_six_exceptions():
    p = _paragraphe()
    assert "`python -m rosetta.addons.maps`" in p
    for a in SANS_MAIN:
        assert f"`{a}`" in p
    assert "no `__main__` block" in p


def test_paragraphe_stdio_n_est_pas_tronque_ni_doublonne():
    p = _paragraphe()
    assert p.count("python -m rosetta.addons.maps") == 1
    assert "`GET /health`" in p
    assert not re.search(r"^\(`python -m [^`]+`\)\.\s*$", p, re.M)


def test_exceptions_du_readme_sont_celles_du_code():
    for a in SANS_MAIN:
        assert "__main__" not in (ROOT / "src/rosetta/addons" / f"{a}.py").read_text()
    for a in AVEC_MAIN:
        assert "__main__" in (ROOT / "src/rosetta/addons" / f"{a}.py").read_text()
