"""Feature rosetta-preuve-tests, level ensemble: the proof of test is the attestation, not a CI job.

Written from the need (the Livre and its exit criterion), not from the implementation.
Expected values are literal. The attestation job's shell is played for real against
throwaway git repositories and a stub gh (no network), the jq filter being the real jq.
"""
import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
PUBLISH = ROOT / ".github" / "workflows" / "docker-publish.yml"


def _load(path):
    doc = yaml.safe_load(path.read_text())
    triggers = doc.pop(True, doc.pop("on", {}))  # YAML 1.1 reads on as True
    if isinstance(triggers, list):
        triggers = {t: None for t in triggers}
    elif isinstance(triggers, str):
        triggers = {triggers: None}
    doc["triggers"] = triggers
    return doc


def _all_run_scripts(doc):
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if "run" in step:
                yield step["run"]


def _declared():
    return yaml.safe_load((ROOT / ".sdlc.yaml").read_text())["tests"]


# --- .sdlc.yaml declares the suite -------------------------------------------

def test_sdlc_yaml_declares_the_four_fields_of_the_suite():
    assert set(_declared()) >= {"commande", "rapport", "format", "delai"}


def test_declared_format_is_tap_or_junit_and_delay_is_a_duration():
    tests = _declared()
    assert tests["format"] in {"tap", "junit"}
    assert re.fullmatch(r"\d+[smh]", str(tests["delai"]))


def test_declared_command_is_an_executable_file_of_the_repo():
    path = ROOT / _declared()["commande"]
    assert path.is_file()
    assert path.stat().st_mode & stat.S_IXUSR


def test_declared_report_is_written_by_the_command_and_not_tracked():
    tests = _declared()
    assert tests["rapport"] in (ROOT / tests["commande"]).read_text()
    assert tests["rapport"] in (ROOT / ".gitignore").read_text().split()


def test_declared_suite_plays_lint_and_tests_with_coverage():
    script = (ROOT / _declared()["commande"]).read_text()
    assert "ruff check" in script
    assert "pytest" in script and "--cov" in script


# --- no workflow plays the suite ---------------------------------------------

def test_a_publication_workflow_exists():
    assert PUBLISH in WORKFLOWS


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_no_workflow_triggers_on_pull_request_or_branch_push(workflow):
    triggers = _load(workflow)["triggers"]
    for forbidden in ("pull_request", "pull_request_target", "merge_group", "release", "schedule"):
        assert forbidden not in triggers
    push = triggers.get("push") or {}
    assert "branches" not in push
    assert "branches-ignore" not in push


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_no_workflow_plays_lint_or_tests(workflow):
    for script in _all_run_scripts(_load(workflow)):
        assert "pytest" not in script
        assert "ruff" not in script
        assert "pip install" not in script


def test_push_trigger_is_limited_to_version_tags():
    assert _load(PUBLISH)["triggers"]["push"] == {"tags": ["v*"]}


# --- the image builds only on a v* tag, after the attestation ----------------

def _attestation_job():
    jobs = _load(PUBLISH)["jobs"]
    return next((n, j) for n, j in jobs.items() if "tests-complets" in str(j) and n != "build")


def test_build_waits_for_the_attestation_job():
    needs = _load(PUBLISH)["jobs"]["build"]["needs"]
    needs = [needs] if isinstance(needs, str) else needs
    assert _attestation_job()[0] in needs


def test_image_is_never_built_outside_a_version_tag():
    """Manual dispatch must not let a branch (main included) build an image."""
    doc = _load(PUBLISH)
    if "workflow_dispatch" not in doc["triggers"]:
        return
    gate = " ".join(str(job.get("if", "")) for job in doc["jobs"].values())
    assert "refs/tags/v" in gate


def test_image_tags_come_from_the_version_tag_not_from_a_branch():
    steps = _load(PUBLISH)["jobs"]["build"]["steps"]
    meta = next(s for s in steps if str(s.get("uses", "")).startswith("docker/metadata-action"))
    tags = meta["with"]["tags"]
    assert "type=semver" in tags
    for forbidden in ("event=branch", "event=pr", "value=main"):
        assert forbidden not in tags


# --- CLAUDE.md states the rule ------------------------------------------------

def test_claude_md_states_the_rule():
    text = (ROOT / "CLAUDE.md").read_text()
    assert "tests-complets" in text
    assert "attester --publier" in text
    assert "v*" in text
    assert "scripts/suite-attestee.sh" in text


# --- the attestation job, played for real ------------------------------------

STUB_GH = """#!/usr/bin/env python3
import os, subprocess, sys
args = sys.argv[1:]
sha = args[1].split("/")[-2]
jq = args[args.index("--jq") + 1]
path = os.path.join(os.environ["STATUS_DIR"], sha)
body = open(path).read() if os.path.exists(path) else '{"statuses": []}'
sys.stdout.write(subprocess.run(["jq", "-r", jq], input=body, capture_output=True, text=True, check=True).stdout)
"""


def _git(cwd, *args):
    ident = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null"}
    varenv = dict(os.environ, **ident)
    return subprocess.run(["git", *args], cwd=cwd, env=varenv, check=True, capture_output=True, text=True).stdout.strip()


# --- playing the attestation shell against a throwaway repo -----------------

def _status_body(state):
    statuses = [dict(context="build", state="success"), dict(context="tests-complets", state=state)]
    return json.dumps(dict(statuses=statuses))


def _write(path, text):
    return path.write_text(text)


def _play(tmp_path, statuses, same_tree_parent):
    # statuses maps parent/head to a state, a missing key means no status at all
    repo = tmp_path / "repo"
    sdir = tmp_path / "status"
    bindir = tmp_path / "bin"
    _ = [os.makedirs(folder) for folder in [repo, sdir, bindir]]
    _git(repo, "init", "-q")
    _write(repo / "x", "1")
    _git(repo, "add", "x")
    _git(repo, "commit", "-qm", "parent")
    parent = _git(repo, "rev-parse", "HEAD")
    _write(repo / "x", ["2", "1"][same_tree_parent])
    _git(repo, "commit", "-qa", "--allow-empty", "-m", "head")
    head = _git(repo, "rev-parse", "HEAD")
    shas = dict(parent=parent, head=head)
    _ = [_write(sdir / shas[which], _status_body(state)) for which, state in statuses.items()]
    gh = bindir / "gh"
    _write(gh, STUB_GH)
    os.chmod(gh, 0o755)
    shell_text = next(s["run"] for s in _attestation_job()[1]["steps"] if "run" in s)
    extra = dict(STATUS_DIR=str(sdir), GH_TOKEN="x", REPO="o/r")
    varenv = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"], **extra)
    return subprocess.run(["bash", "-c", shell_text], cwd=repo, env=varenv, capture_output=True, text=True)


def test_attestation_passes_when_the_tagged_commit_is_attested(tmp_path):
    assert _play(tmp_path, dict(head="success"), same_tree_parent=False).returncode == 0


def test_attestation_passes_when_a_parent_of_same_tree_is_attested(tmp_path):
    assert _play(tmp_path, dict(parent="success"), same_tree_parent=True).returncode == 0


def test_attestation_fails_when_no_commit_is_attested(tmp_path):
    result = _play(tmp_path, dict(), same_tree_parent=True)
    assert result.returncode == 1
    assert "tests-complets" in result.stdout + result.stderr


def test_attestation_fails_when_the_only_attested_parent_has_another_tree(tmp_path):
    assert _play(tmp_path, dict(parent="success"), same_tree_parent=False).returncode == 1


@pytest.mark.parametrize("state", ["failure", "pending", "error"])
def test_attestation_fails_when_the_status_is_not_success(tmp_path, state):
    assert _play(tmp_path, dict(head=state), same_tree_parent=False).returncode == 1
