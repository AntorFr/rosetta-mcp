"""Tranche `rosetta-qualite-recommandations-sujets` : forme de docs/recommandations.md (AD-recommandations-1)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = (ROOT / "docs" / "recommandations.md").read_text()

SUJETS = {  # id -> mot-clé du sujet attendu dans le titre
    "R-1": "User-Agent", "R-2": "quotas", "R-3": "yt-dlp", "R-4": "digest",
    "R-5": "Actions", "R-6": "mypy", "R-7": "google.py",
}
RELEVES = {"R-8", "R-9", "R-10", "R-11"}  # relevés des tranches précédentes, après les sept
COUT = {"S": 0, "M": 1, "L": 2}


def _toc():
    rows = re.findall(r"^\| (R-\d+) \| (.+?) \| ([SML]) \| (.+?) \|$", DOC, re.M)
    return rows


def _entrees():
    parts = re.split(r"^## (R-\d+) — (.+)$", DOC, flags=re.M)
    return {parts[i]: (parts[i + 1], parts[i + 2]) for i in range(1, len(parts), 3)}


def test_les_sept_sujets_ont_une_entree_et_une_ligne_de_table():
    entrees, toc = _entrees(), _toc()
    assert set(entrees) == set(SUJETS) | RELEVES == {r[0] for r in toc}
    for rid, mot in SUJETS.items():
        assert mot in entrees[rid][0], rid
    assert list(entrees)[:7] == list(SUJETS)  # les sept d'abord, jamais réordonnés


def test_table_et_entrees_rangees_par_classe_croissante():
    ids = [r[0] for r in _toc()]
    classes = [COUT[r[2]] for r in _toc()]
    assert classes[:7] == sorted(classes[:7])  # deux groupes, chacun du moins au plus coûteux
    assert classes[7:] == sorted(classes[7:])
    assert ids == list(_entrees())  # même ordre dans le corps


def test_table_donne_option_recommandee_non_vide():
    for rid, _, _, opt in _toc():
        assert re.match(r"[A-Z] — \S", opt), rid


def test_chaque_entree_a_les_cinq_parties_dans_l_ordre():
    for rid, (_, corps) in _entrees().items():
        pos = [corps.find(m) for m in ("**Finding.**", "**Why it matters.**", "**Options.**",
                                       "**Recommendation.**", "**Status.**")]
        assert all(p >= 0 for p in pos), rid
        assert pos == sorted(pos), rid
        assert re.search(r"\*\*Status\.\*\* `open`", corps), rid


def test_au_moins_deux_options_et_recommandation_parmi_elles():
    toc = {r[0]: r[3][0] for r in _toc()}
    for rid, (_, corps) in _entrees().items():
        options = corps.split("**Options.**")[1].split("**Recommendation.**")[0]
        lettres = re.findall(r"^- ([A-Z])\. ", options, re.M)
        assert len(lettres) >= 2, rid
        assert toc[rid] in lettres, rid


def test_chaque_constat_cite_un_emplacement():
    for rid, (_, corps) in _entrees().items():
        constat = corps.split("**Why it matters.**")[0]
        assert re.search(r"`[\w./-]+(:\d+)?", constat) and re.search(r"\d", constat), rid


def test_constats_vrais_dans_le_code():
    food = (ROOT / "src/rosetta/addons/food.py").read_text().splitlines()
    assert "OFF_USER_AGENT" in food[67]
    trace = (ROOT / "src/rosetta/addons/trace.py").read_text().splitlines()
    assert "OVERPASS_USER_AGENT" in trace[553]
    assert "claude-code/2.1.274" in (ROOT / "src/rosetta/addons/quotas.py").read_text().splitlines()[134]
    assert "yt-dlp>=2025.1" in (ROOT / "pyproject.toml").read_text().splitlines()[29]
    dock = (ROOT / "Dockerfile").read_text().splitlines()
    assert dock[5].startswith("FROM python:3.12-slim") and dock[14].startswith("FROM python:3.12-slim")
    wf = (ROOT / ".github/workflows/docker-publish.yml").read_text()
    assert len(re.findall(r"uses: \S+@v\d", wf)) == 7  # 6 Actions, checkout deux fois (attestation, build)
