"""scripts/rejouer.sh: replays only the named tests, same JUnit report, no lint, no coverage.

Played for real in a throwaway project (a copy of the script, tiny tests, the repo's
.venv-suite linked in), so the suite never rewrites the repo's own resultats.xml.
"""
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "rejouer.sh"
VENV = ROOT / ".venv-suite"

pytestmark = pytest.mark.skipif(not (VENV / "bin" / "pytest").exists(), reason="needs .venv-suite (made by suite-attestee.sh)")

SAMPLE = '''
import pytest

def test_alpha():
    pass

def test_beta():
    pass

def test_rouge():
    assert False

@pytest.mark.parametrize("n", [1, 2])
def test_param(n):
    pass

class TestC:
    def test_m(self):
        pass
'''


@pytest.fixture
def projet(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "rejouer.sh")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_sample.py").write_text(SAMPLE)
    (tmp_path / ".venv-suite").symlink_to(VENV)
    return tmp_path


def rejouer(projet, *noms):
    p = subprocess.run(["bash", str(projet / "scripts" / "rejouer.sh"), *noms], cwd=projet, capture_output=True, text=True)
    report = projet / "resultats.xml"
    cases = [(c.get("classname"), c.get("name")) for c in ET.parse(report).getroot().iter("testcase")] if report.exists() else []
    return p, cases


@pytest.mark.parametrize("nom", ["test_alpha", "tests.test_sample.test_alpha", "'test_alpha'", '"tests.test_sample.test_alpha"'])
def test_one_named_test_in_three_forms_runs_alone_and_passes(projet, nom):
    p, cases = rejouer(projet, nom)
    assert p.returncode == 0, p.stdout + p.stderr
    assert cases == [("tests.test_sample", "test_alpha")]
    assert "ruff" not in (p.stdout + p.stderr).lower()
    assert "coverage" not in p.stdout.lower()


def test_unknown_name_exits_2_and_never_a_green_at_zero_test(projet):
    p, cases = rejouer(projet, "test_nope")
    assert p.returncode == 2
    assert cases == []


def test_one_unknown_among_known_names_still_fails(projet):
    p, cases = rejouer(projet, "test_alpha", "test_nope")
    assert p.returncode == 2
    assert cases == []


def test_no_argument_fails(projet):
    p, _ = rejouer(projet)
    assert p.returncode == 2


def test_names_separated_by_spaces_in_one_argument(projet):
    p, cases = rejouer(projet, "test_alpha test_beta")
    assert p.returncode == 0
    assert sorted(n for _, n in cases) == ["test_alpha", "test_beta"]


def test_a_red_test_makes_the_script_fail_and_is_reported(projet):
    p, cases = rejouer(projet, "test_rouge")
    assert p.returncode != 0
    assert cases == [("tests.test_sample", "test_rouge")]


def test_method_of_a_class_by_prefixed_name(projet):
    p, cases = rejouer(projet, "tests.test_sample.TestC.test_m")
    assert p.returncode == 0
    assert cases == [("tests.test_sample.TestC", "test_m")]


def test_parametrized_case_by_its_junit_name_runs_that_case_only(projet):
    p, cases = rejouer(projet, "tests.test_sample.test_param[2]")
    assert p.returncode == 0
    assert cases == [("tests.test_sample", "test_param[2]")]


def test_a_module_name_is_not_a_test_name(projet):
    p, cases = rejouer(projet, "test_sample")
    assert p.returncode == 2
    assert cases == []


def test_sdlc_yaml_declares_local_and_rejouer():
    tests = yaml.safe_load((ROOT / ".sdlc.yaml").read_text())["tests"]
    assert tests["local"] == tests["commande"] == "scripts/suite-attestee.sh"
    assert tests["rejouer"] == "scripts/rejouer.sh {tests}"
    assert (ROOT / "scripts" / "rejouer.sh").stat().st_mode & 0o100
