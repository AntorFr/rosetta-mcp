"""B-7 / B-5 (lint) / B-6 (coverage): lint and coverage are configured, enforced, ; the image build is gated by the attestation (tests/test_publication.py).

Expected values come from the need, written literally.
"""
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
WORKFLOW = ROOT / ".github" / "workflows" / "docker-publish.yml"


def _pyproject():
    return tomllib.loads(PYPROJECT.read_text())


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def _run(*args, cwd=ROOT):
    return subprocess.run([sys.executable, "-m", *args], cwd=cwd, capture_output=True, text=True, timeout=600)


def test_lint_selects_at_least_the_five_rule_families():
    selected = set(_pyproject()["tool"]["ruff"]["lint"]["select"])
    assert {"E", "F", "W", "I", "B"} <= selected


def test_lint_does_not_ignore_a_whole_rule_family():
    lint = _pyproject()["tool"]["ruff"]["lint"]
    ignored = set(lint.get("ignore", []))
    for fam in ("E", "F", "W", "I", "B"):
        assert fam not in ignored
    for rules in lint.get("per-file-ignores", {}).values():
        assert not ({"E", "F", "W", "I", "B", "ALL"} & set(rules))


def test_every_ignore_entry_carries_a_reason_comment():
    text = PYPROJECT.read_text().splitlines()
    in_lint = False
    entries = 0
    for i, line in enumerate(text):
        if line.startswith("["):
            in_lint = line.startswith("[tool.ruff.lint")
            continue
        if not in_lint:
            continue
        is_entry = re.match(r'\s*"[^"]+"\s*=\s*\[', line) or (re.match(r"\s*ignore\s*=", line))
        if is_entry:
            entries += 1
            above = text[i - 1].lstrip().startswith("#") if i else False
            assert "#" in line or above, f"ignore entry without reason: {line!r}"
    # items inside a multi-line ignore list
    block = re.search(r"^ignore\s*=\s*\[(.*?)\]", PYPROJECT.read_text(), re.S | re.M)
    if block:
        for item in block.group(1).splitlines():
            if item.strip().startswith('"'):
                assert "#" in item, f"ignore item without reason: {item!r}"


def test_ruff_check_reports_nothing_on_the_repository():
    r = _run("ruff", "check", ".")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "All checks passed" in r.stdout


def test_ruff_check_catches_a_violation_of_the_selected_rules(tmp_path):
    # the configuration really bites: an unused import (F401) and a bad import order (I001) are reported
    (tmp_path / "pyproject.toml").write_text(PYPROJECT.read_text())
    (tmp_path / "bad.py").write_text("import sys\nimport os\n")
    r = _run("ruff", "check", "bad.py", cwd=tmp_path)
    assert r.returncode != 0
    assert "F401" in r.stdout
    assert "I001" in r.stdout


def test_coverage_floor_is_inscribed_and_not_trivial():
    floor = _pyproject()["tool"]["coverage"]["report"]["fail_under"]
    assert 50 <= floor <= 100


def test_pytest_cov_fails_when_coverage_is_below_the_floor(tmp_path):
    # one small test file covers far less than the floor: the run must fail on coverage, not on tests
    env_file = tmp_path / ".cov"
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--cov", "-q", "-p", "no:cacheprovider", "tests/test_hub.py"],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
        env={**__import__("os").environ, "COVERAGE_FILE": str(env_file)},
    )
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "FAIL Required test coverage of 85.0% not reached" in out
