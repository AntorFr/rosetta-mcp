"""git addon: the smart-HTTP proxy's ref rules, parsed off real pkt-line bytes.

Everything here is about what the proxy REFUSES, because that is the only part
GitHub cannot do for us: the wire protocol carries no force flag, so a relayed
push would silently accept a rewritten history.
"""

import asyncio
import base64
import json

import httpx
import pytest
from starlette.requests import Request

from rosetta.addons import git, github
from rosetta.auth import current_claims

ZERO = "0" * 40
A = "a" * 40
B = "b" * 40


def run(coro):
    return asyncio.run(coro)


def pkt(line: bytes) -> bytes:
    """Encode one pkt-line (4 hex length chars covering themselves)."""
    return b"%04x%s" % (len(line) + 4, line)


FLUSH = b"0000"


@pytest.fixture
def enrolled(tmp_path, monkeypatch):
    monkeypatch.setenv("ROSETTA_GITHUB_DATA", str(tmp_path))
    monkeypatch.setenv("GITHUB_CLIENT_ID", "Iv23licid")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "csec")
    monkeypatch.setenv("GITHUB_OWNER", "AntorFr")
    monkeypatch.delenv("ROSETTA_GIT_REPOS", raising=False)
    github._token_cache.clear()
    users = tmp_path / "users"
    users.mkdir()
    (users / "sebastien.json").write_text(json.dumps({
        "sub": "sebastien", "refresh_token": "rt-1", "enrolled_at": 0,
    }))
    current_claims.set({"sub": "sebastien"})
    yield tmp_path
    github._transport = None
    git._transport = None
    current_claims.set(None)


def github_api(compare_status: str | None, calls: list | None = None):
    """Mock GitHub: the OAuth refresh, plus /compare answering `compare_status`."""
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if request.url.path.endswith("/access_token"):
            return httpx.Response(200, json={"access_token": "gho_live", "expires_in": 28800})
        if "/compare/" in request.url.path:
            if compare_status is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"status": compare_status})
        return httpx.Response(500, json={"message": f"unexpected {request.url}"})
    github._transport = httpx.MockTransport(handler)
    return handler


# --------------------------------------------------------------------------
# pkt-line parsing
# --------------------------------------------------------------------------

def test_parse_reads_commands_until_the_flush_packet():
    body = (pkt(b"%s %s refs/heads/main\x00report-status\n" % (A.encode(), B.encode()))
            + pkt(b"%s %s refs/heads/side\n" % (ZERO.encode(), B.encode()))
            + FLUSH + b"PACK-binary-garbage")
    commands, complete = git._parse_commands(body)
    assert complete is True
    assert commands == [(A, B, "refs/heads/main"), (ZERO, B, "refs/heads/side")]


def test_parse_reports_incomplete_when_the_flush_has_not_arrived():
    body = pkt(b"%s %s refs/heads/main\n" % (A.encode(), B.encode()))
    commands, complete = git._parse_commands(body)
    assert complete is False
    assert commands == [(A, B, "refs/heads/main")]


def test_parse_refuses_a_body_that_is_not_pkt_line():
    with pytest.raises(ValueError):
        git._parse_commands(b"this is not a pkt-line at all")


# --------------------------------------------------------------------------
# Ref rules — what the proxy refuses
# --------------------------------------------------------------------------

def test_deleting_a_ref_is_refused(enrolled):
    github_api("ahead")
    reason = run(git._check_commands("AntorFr/x", [(A, ZERO, "refs/heads/main")]))
    assert reason and "suppression" in reason


def test_a_ref_outside_heads_and_tags_is_refused(enrolled):
    github_api("ahead")
    reason = run(git._check_commands("AntorFr/x", [(ZERO, B, "refs/pull/7/head")]))
    assert reason and "hors périmètre" in reason


def test_moving_an_existing_tag_is_refused(enrolled):
    github_api("ahead")
    reason = run(git._check_commands("AntorFr/x", [(A, B, "refs/tags/agent-gw-v0.57.2")]))
    assert reason and "déplace jamais" in reason


def test_creating_a_tag_is_allowed_without_asking_github(enrolled):
    calls: list = []
    github_api("ahead", calls)
    reason = run(git._check_commands("AntorFr/x", [(ZERO, B, "refs/tags/v1.0.0")]))
    assert reason is None
    # A brand-new tag has no ancestry to check: no /compare call at all.
    assert not [c for c in calls if "/compare/" in c]


def test_a_new_branch_is_allowed(enrolled):
    github_api("ahead")
    assert run(git._check_commands("AntorFr/x", [(ZERO, B, "refs/heads/feature")])) is None


def test_a_fast_forward_push_is_allowed(enrolled):
    github_api("ahead")
    assert run(git._check_commands("AntorFr/x", [(A, B, "refs/heads/main")])) is None


def test_a_non_fast_forward_push_is_refused(enrolled):
    # This is THE case GitHub would happily accept on an unprotected branch.
    github_api("diverged")
    reason = run(git._check_commands("AntorFr/x", [(A, B, "refs/heads/main")]))
    assert reason and "non fast-forward" in reason


def test_an_unverifiable_ancestry_is_refused_not_waved_through(enrolled):
    github_api(None)  # /compare answers 404
    reason = run(git._check_commands("AntorFr/x", [(A, B, "refs/heads/main")]))
    assert reason and "refus par sécurité" in reason


def test_an_empty_command_list_is_refused(enrolled):
    github_api("ahead")
    assert run(git._check_commands("AntorFr/x", [])) is not None


# --------------------------------------------------------------------------
# The envelope handed upstream
# --------------------------------------------------------------------------

def test_the_upstream_credential_is_basic_not_bearer(enrolled):
    """One token, two doors, two envelopes — and only one is right here.

    `api.github.com` takes the App token as a Bearer; the smart-HTTP endpoints on
    `github.com` take it ONLY as Basic `x-access-token:<token>` and answer a bare
    401 `invalid credentials` otherwise. The proxy streams upstream status and
    body straight through, so that refusal lands in the pusher's terminal looking
    exactly like a hub refusal — which is what made the first push fail with a
    hub that was, itself, perfectly configured.
    """
    github_api("ahead")
    headers = run(git._github_headers())
    scheme, _, credential = headers["Authorization"].partition(" ")
    assert scheme == "Basic", "a Bearer here is a 401 nobody can debug from the pod"
    user, _, token = base64.b64decode(credential).decode().partition(":")
    assert (user, token) == ("x-access-token", "gho_live")


# --------------------------------------------------------------------------
# The route: a refused push must never reach GitHub
# --------------------------------------------------------------------------

def make_request(body: bytes, headers: dict | None = None) -> Request:
    state = {"sent": False}

    async def receive():
        if state["sent"]:
            return {"type": "http.disconnect"}
        state["sent"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/git/AntorFr/agent-pods/git-receive-pack",
        "raw_path": b"/git/AntorFr/agent-pods/git-receive-pack",
        "headers": raw,
        "query_string": b"",
        "path_params": {"repo": "AntorFr/agent-pods"},
    }
    return Request(scope, receive)


def test_a_compressed_body_is_refused_because_inspection_would_be_blind(enrolled):
    github_api("ahead")
    response = run(git.receive_pack(make_request(b"\x1f\x8b", {"content-encoding": "gzip"})))
    assert response.status_code == 403
    assert b"compress" in response.body


def test_a_refused_push_never_calls_github_git(enrolled):
    calls: list = []
    github_api("diverged", calls)
    # git._transport stays None: any attempt to reach github.com would explode
    # on a real connection, which is exactly the assertion we want.
    body = (pkt(b"%s %s refs/heads/main\x00report-status\n" % (A.encode(), B.encode()))
            + FLUSH + b"PACKnope")
    response = run(git.receive_pack(make_request(body)))
    assert response.status_code == 403
    assert b"fast-forward" in response.body
    assert not [c for c in calls if "github.com/AntorFr" in c and ".git" in c]


def test_a_truncated_command_list_is_refused(enrolled):
    github_api("ahead")
    body = pkt(b"%s %s refs/heads/main\n" % (A.encode(), B.encode()))  # no flush
    response = run(git.receive_pack(make_request(body)))
    assert response.status_code == 400


def test_repo_allowlist_closes_the_door(enrolled, monkeypatch):
    monkeypatch.setenv("ROSETTA_GIT_REPOS", "AntorFr/other-repo")
    github_api("ahead")
    response = run(git.receive_pack(make_request(FLUSH)))
    assert response.status_code == 403
    assert b"ROSETTA_GIT_REPOS" in response.body
