"""AD-preuve-1 (3): the image is built only on a `v*` tag, after the `tests-complets` attestation is checked.

Expected values come from the need, written literally. The attestation step is played for real
(its shell, its `jq` filter) against throwaway git repositories and a stub `gh`.
"""
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "docker-publish.yml"


def _wf():
    return yaml.safe_load(WORKFLOW.read_text())


def _triggers():
    return _wf()[True]  # YAML 1.1 reads `on` as True


def _meta_tags():
    steps = _wf()["jobs"]["build"]["steps"]
    meta = next(s for s in steps if str(s.get("uses", "")).startswith("docker/metadata-action"))
    return meta["with"]["tags"]


def _attestation_script():
    steps = _wf()["jobs"]["attestation"]["steps"]
    return next(s["run"] for s in steps if "run" in s)


# --- triggers ---------------------------------------------------------------

def test_triggers_are_version_tags_and_manual_dispatch_only():
    trig = _triggers()
    assert set(trig) == {"push", "workflow_dispatch"}
    assert trig["push"] == {"tags": ["v*"]}


def test_no_pull_request_release_or_branch_trigger():
    text = WORKFLOW.read_text()
    assert "pull_request" not in text
    assert "release:" not in text
    assert "branches" not in _triggers()["push"]


# --- no test in the workflow --------------------------------------------------

def test_workflow_plays_no_lint_nor_test():
    text = WORKFLOW.read_text()
    assert "pytest" not in text
    assert "ruff" not in text
    assert "test" not in _wf()["jobs"]


# --- build gated by the attestation ------------------------------------------

def test_build_needs_the_attestation_job():
    needs = _wf()["jobs"]["build"]["needs"]
    needs = [needs] if isinstance(needs, str) else needs
    assert "attestation" in needs


def test_attestation_job_checks_out_two_commits_deep_and_cannot_fail_silently():
    job = _wf()["jobs"]["attestation"]
    assert not job.get("continue-on-error")
    checkout = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["fetch-depth"] == 2
    assert "|| true" not in _attestation_script()


# --- image tags ---------------------------------------------------------------

def test_image_tags_are_semver_and_sha_without_branch_tag_nor_latest_override():
    tags = _meta_tags()
    lines = [ln.strip() for ln in tags.splitlines() if ln.strip()]
    assert "type=semver,pattern={{version}}" in lines
    assert "type=semver,pattern={{major}}.{{minor}}" in lines
    assert "type=semver,pattern={{major}}" in lines
    assert "type=sha,prefix=sha-" in lines
    assert "type=ref,event=branch" not in tags
    assert "latest=false" not in WORKFLOW.read_text()


def test_multi_arch_only_on_stable_versions_is_unchanged():
    steps = _wf()["jobs"]["build"]["steps"]
    build = next(s for s in steps if str(s.get("uses", "")).startswith("docker/build-push-action"))
    platforms = " ".join(str(build["with"]["platforms"]).split())
    assert platforms == (
        "${{ (startsWith(github.ref, 'refs/tags/v') && !contains(github.ref, '-')) "
        "&& 'linux/amd64,linux/arm64' || 'linux/amd64' }}"
    )
    assert build["with"]["push"] is True


# --- the attestation step, played ---------------------------------------------

def _git(repo, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, env=env, check=True)
    return r.stdout.strip()


@pytest.fixture
def play(tmp_path):
    """play(statuses) -> (returncode, output); statuses maps a revision of the repo to its API payload."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    answers = tmp_path / "answers"
    answers.mkdir()
    gh = bindir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        "# stub of: gh api repos/<repo>/commits/<sha>/status --jq <filter>\n"
        'sha="${2#*/commits/}"; sha="${sha%/status}"\n'
        f'f="{answers}/$sha.json"\n'
        f'[ -f "$f" ] || f="{answers}/default.json"\n'
        'jq -r "$4" "$f"\n'
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)

    def run(statuses):
        (answers / "default.json").write_text(json.dumps({"state": "pending", "statuses": []}))
        for rev, payload in statuses.items():
            (answers / f"{_git(repo, 'rev-parse', rev)}.json").write_text(json.dumps(payload))
        script = tmp_path / "step.sh"
        script.write_text(_attestation_script())
        env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "GH_TOKEN": "x", "REPO": "o/r"}
        r = subprocess.run(["bash", str(script)], cwd=repo, capture_output=True, text=True, env=env, timeout=60)
        return r.returncode, r.stdout + r.stderr

    run.repo = repo
    return run


def _commit(repo, name, content):
    (repo / name).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", content)


def _status(context="tests-complets", state="success"):
    return {"state": state, "statuses": [{"context": context, "state": state}]}


def test_attested_commit_passes(play):
    _commit(play.repo, "a", "1")
    code, out = play({"HEAD": _status()})
    assert code == 0, out
    assert "attested" in out


def test_commit_without_status_fails_with_the_error_message(play):
    _commit(play.repo, "a", "1")
    code, out = play({})
    assert code == 1
    assert "::error::no tests-complets attestation" in out
    assert "attester --publier" in out


@pytest.mark.parametrize("payload", [
    _status(state="failure"),
    _status(state="pending"),
    _status(state="error"),
    _status(context="autre-contexte"),
])
def test_status_that_is_not_a_tests_complets_success_fails(play, payload):
    _commit(play.repo, "a", "1")
    code, out = play({"HEAD": payload})
    assert code == 1, out


def test_success_of_tests_complets_is_found_among_other_contexts(play):
    _commit(play.repo, "a", "1")
    payload = {"state": "failure", "statuses": [
        {"context": "ruleset", "state": "failure"}, {"context": "tests-complets", "state": "success"}]}
    code, out = play({"HEAD": payload})
    assert code == 0, out


def test_merge_commit_with_the_attested_parents_tree_passes(play):
    repo = play.repo
    _commit(repo, "a", "1")
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "b", "2")
    attested = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "feature")  # main had not moved: same tree as feature
    assert _git(repo, "rev-parse", "HEAD^{tree}") == _git(repo, "rev-parse", attested + "^{tree}")
    code, out = play({attested: _status()})
    assert code == 0, out


def test_attestation_of_a_parent_with_another_tree_does_not_count(play):
    repo = play.repo
    _commit(repo, "a", "1")
    attested_old = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "b", "2")  # a later commit, different tree, not attested
    code, out = play({attested_old: _status()})
    assert code == 1
    assert "::error::" in out
