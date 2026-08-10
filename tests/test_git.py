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


def github_api(compare_status: str | None = "ahead", calls: list | None = None,
               promotion: str = "ok"):
    """Mock the GitHub REST API: OAuth refresh, ref promotion, scratch cleanup.

    `promotion` is what `PATCH /git/refs/...` answers — "ok", or "non-ff" for the
    422 GitHub returns when the update would rewrite history.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(f"{request.method} {request.url}")
        if request.url.path.endswith("/access_token"):
            return httpx.Response(200, json={"access_token": "gho_live", "expires_in": 28800})
        if "/git/refs/" in request.url.path:
            if request.method == "DELETE":
                return httpx.Response(204)
            if request.method == "PATCH":
                if promotion == "non-ff":
                    return httpx.Response(422, json={"message": "Update is not a fast forward"})
                return httpx.Response(200, json={"ref": "refs/heads/main"})
        if "/compare/" in request.url.path:
            if compare_status is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"status": compare_status})
        return httpx.Response(500, json={"message": f"unexpected {request.url}"})
    github._transport = httpx.MockTransport(handler)
    return handler


def github_git(calls: list | None = None, unpack_ok: bool = True):
    """Mock github.com's smart-HTTP side — where the pack actually lands."""
    async def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append((str(request.url), await request.aread()))
        report = b"unpack ok\n" if unpack_ok else b"unpack index-pack failed\n"
        return httpx.Response(200, content=pkt(report) + FLUSH)
    git._transport = httpx.MockTransport(handler)
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

def test_deleting_a_ref_is_refused():
    reason = git._check_commands([(A, ZERO, "refs/heads/main")])
    assert reason and "suppression" in reason


def test_a_ref_outside_heads_and_tags_is_refused():
    reason = git._check_commands([(ZERO, B, "refs/pull/7/head")])
    assert reason and "hors périmètre" in reason


def test_moving_an_existing_tag_is_refused():
    reason = git._check_commands([(A, B, "refs/tags/agent-gw-v0.57.2")])
    assert reason and "déplace jamais" in reason


def test_creating_a_tag_is_allowed():
    assert git._check_commands([(ZERO, B, "refs/tags/v1.0.0")]) is None


def test_a_new_branch_is_allowed():
    assert git._check_commands([(ZERO, B, "refs/heads/feature")]) is None


def test_an_empty_command_list_is_refused():
    assert git._check_commands([]) is not None


def test_ancestry_is_no_longer_judged_here_and_that_is_the_fix():
    """The old rule asked `/compare/<old>...<new>` BEFORE relaying the pack.

    `<new>` is the commit being pushed, so GitHub had never heard of it and
    answered 404 — which the proxy read as "ancestry unverifiable" and refused.
    Every update of an existing branch was rejected; only creations got through.
    The judgement now happens in `_promote`, where GitHub can actually answer.
    """
    assert git._check_commands([(A, B, "refs/heads/main")]) is None


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
    github_api(calls=calls)
    # git._transport stays None: any attempt to reach github.com would explode
    # on a real connection, which is exactly the assertion we want.
    body = (pkt(b"%s %s refs/heads/main\x00report-status\n" % (A.encode(), ZERO.encode()))
            + FLUSH + b"PACKnope")
    response = run(git.receive_pack(make_request(body)))
    assert b"ng refs/heads/main" in response.body and b"suppression" in response.body
    assert not [c for c in calls if "github.com/AntorFr" in c]


# --------------------------------------------------------------------------
# Promotion — where ancestry is actually decided
# --------------------------------------------------------------------------

def push(new=B, old=A, ref=b"refs/heads/main", caps=b"report-status"):
    line = b"%s %s %s\x00%s\n" % (old.encode(), new.encode(), ref, caps)
    return make_request(pkt(line) + FLUSH + b"PACK-payload")


def test_a_branch_update_lands_on_a_scratch_ref_before_the_real_one(enrolled):
    api, wire = [], []
    github_api(calls=api)
    github_git(calls=wire)
    response = run(git.receive_pack(push()))

    # The pack went to a throwaway ref, created from zero: nothing to overwrite.
    (url, sent), = wire
    assert url.endswith("/AntorFr/agent-pods.git/git-receive-pack")
    assert b"rosetta-scratch/" in sent and sent.startswith(b"0"), sent[:80]
    assert ZERO.encode() + b" " + B.encode() in sent
    # ...and the pack itself was streamed through untouched.
    assert sent.endswith(b"PACK-payload")

    # Then the real ref was promoted, explicitly WITHOUT force.
    promote, = [c for c in api if c.startswith("PATCH")]
    assert promote.endswith("/repos/AntorFr/agent-pods/git/refs/heads/main")
    assert [c for c in api if c.startswith("DELETE") and "rosetta-scratch" in c]
    assert b"ok refs/heads/main" in response.body


def test_the_promotion_never_forces(enrolled):
    """`force: false` is the whole guarantee — GitHub itself refuses a rewrite."""
    sent: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_token"):
            return httpx.Response(200, json={"access_token": "gho_live", "expires_in": 28800})
        if request.method == "PATCH":
            sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ref": "refs/heads/main"})
    github._transport = httpx.MockTransport(handler)
    github_git()
    run(git.receive_pack(push()))
    assert sent == [{"sha": B, "force": False}]


def test_a_non_fast_forward_is_refused_by_github_and_reported_on_the_wire(enrolled):
    """THE case GitHub would accept on an unprotected branch — via a bare push."""
    github_api(promotion="non-ff")
    github_git()
    response = run(git.receive_pack(push()))
    assert b"ng refs/heads/main" in response.body
    assert b"non fast-forward" in response.body
    assert b"ok refs/heads/main" not in response.body


def test_the_scratch_ref_is_dropped_even_when_the_promotion_fails(enrolled):
    api: list = []
    github_api(calls=api, promotion="non-ff")
    github_git()
    run(git.receive_pack(push()))
    assert [c for c in api if c.startswith("DELETE") and "rosetta-scratch" in c]


def test_a_creation_takes_the_direct_route_with_no_scratch(enrolled):
    api: list = []
    github_api(calls=api)
    github_git()
    response = run(git.receive_pack(push(old=ZERO, ref=b"refs/heads/feature")))
    assert response.status_code == 200
    assert not [c for c in api if "rosetta-scratch" in c]
    assert not [c for c in api if c.startswith("PATCH")]


def test_a_push_touching_several_refs_at_once_is_refused(enrolled):
    """One promotion per request: two refs would need two scratch round-trips."""
    github_api()
    github_git()
    body = (pkt(b"%s %s refs/heads/main\x00report-status\n" % (A.encode(), B.encode()))
            + pkt(b"%s %s refs/heads/other\n" % (A.encode(), B.encode()))
            + FLUSH + b"PACK")
    response = run(git.receive_pack(make_request(body)))
    assert b"ng " in response.body and "une par requête".encode() in response.body


def test_the_report_is_wrapped_in_the_side_band_when_the_client_asked_for_one(enrolled):
    """A bare report would be unreadable to a client expecting multiplexed data."""
    github_api()
    github_git()
    response = run(git.receive_pack(push(caps=b"report-status side-band-64k")))
    assert response.body[4:5] == b"\x01"  # pkt length, then channel 1
    assert b"ok refs/heads/main" in response.body
    assert response.body.endswith(b"0000")


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
