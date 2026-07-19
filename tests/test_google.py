"""google addon: credential store, enrolment flow, and tool behaviour against
a mocked Google API (httpx.MockTransport - no network)."""

import asyncio
import base64
import json

import httpx
import pytest

from rosetta.addons import google
from rosetta.auth import current_claims


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ROSETTA_GOOGLE_DATA", str(tmp_path))
    (tmp_path / "client_secret.json").write_text(
        json.dumps({"web": {"client_id": "cid", "client_secret": "csec"}})
    )
    return tmp_path


@pytest.fixture
def enrolled(data_dir):
    users = data_dir / "users"
    users.mkdir()
    (users / "sebastien.json").write_text(json.dumps({
        "sub": "sebastien", "refresh_token": "rt-123", "scopes": [], "enrolled_at": 0,
    }))
    google._token_cache.clear()
    current_claims.set({"sub": "sebastien"})
    return data_dir


def mock(handler):
    return httpx.MockTransport(handler)


def run(coro):
    return asyncio.run(coro)


def test_unenrolled_user_gets_actionable_error(data_dir):
    current_claims.set({"sub": "quelqu-un"})
    google._token_cache.clear()
    out = run(google.mail_search("from:test"))
    assert "enroll" in out["error"]


def test_mail_search_and_token_refresh(enrolled, monkeypatch):
    calls = []

    def handler(request):
        calls.append(str(request.url.path))
        if request.url.host == "oauth2.googleapis.com":
            assert b"refresh_token=rt-123" in request.read()
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        return httpx.Response(200, json={
            "id": "m1", "threadId": "t1", "snippet": "extrait",
            "payload": {"headers": [
                {"name": "From", "value": "a@b.c"},
                {"name": "Subject", "value": "Résa"},
                {"name": "Date", "value": "Mon, 20 Jul 2026"},
            ]},
        })

    monkeypatch.setattr(google, "_transport", mock(handler))
    out = run(google.mail_search("from:a@b.c"))
    assert out["messages"][0]["subject"] == "Résa"
    assert out["messages"][0]["thread_id"] == "t1"
    # token endpoint hit exactly once, then cached
    run(google.mail_search("again"))
    assert sum(1 for c in calls if c == "/token") == 1


def test_mail_draft_builds_reply_mime(enrolled, monkeypatch):
    captured = {}

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if "/threads/" in request.url.path:
            return httpx.Response(200, json={"messages": [
                {"payload": {"headers": [{"name": "Message-ID", "value": "<orig@x>"}]}},
            ]})
        captured.update(json.loads(request.read()))
        return httpx.Response(200, json={"id": "d1"})

    monkeypatch.setattr(google, "_transport", mock(handler))
    out = run(google.mail_draft("x@y.z", "Re: Résa", "Bien reçu.", thread_id="t1"))
    assert out["draft_id"] == "d1"
    assert captured["message"]["threadId"] == "t1"
    raw = base64.urlsafe_b64decode(captured["message"]["raw"]).decode()
    assert "To: x@y.z" in raw and "In-Reply-To: <orig@x>" in raw
    assert "Re: =?utf-8?q?R=C3=A9sa?=" in raw or "Re: Résa" in raw


def test_draft_keeps_thread_id_even_if_metadata_fetch_fails(enrolled, monkeypatch):
    """The thread attachment must never depend on the best-effort header fetch."""
    captured = {}

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if "/threads/" in request.url.path:
            return httpx.Response(500, json={})
        captured.update(json.loads(request.read()))
        return httpx.Response(200, json={"id": "d2"})

    monkeypatch.setattr(google, "_transport", mock(handler))
    out = run(google.mail_draft("x@y.z", "Re: Résa", "Corps.", thread_id="t1"))
    assert out["draft_id"] == "d2"
    assert captured["message"]["threadId"] == "t1"
    raw = base64.urlsafe_b64decode(captured["message"]["raw"]).decode()
    assert "In-Reply-To" not in raw  # headers skipped, attachment preserved


def test_store_keyed_on_preferred_username(enrolled, monkeypatch):
    """Authelia access tokens may carry an opaque sub: the username claim wins."""
    current_claims.set({"sub": "opaque-uuid-1234", "preferred_username": "sebastien"})

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        return httpx.Response(200, json={"messages": []})

    monkeypatch.setattr(google, "_transport", mock(handler))
    out = run(google.mail_search("x"))
    assert "error" not in out  # resolved the sebastien.json credential


def test_no_send_tool_exists():
    tool_names = {t.name for t in run(google.mcp.list_tools())}
    assert tool_names == {
        "mail_search", "mail_thread", "mail_draft",
        "calendar_events", "calendar_create", "calendar_update",
    }


def test_state_sign_and_verify(data_dir):
    state = google._sign_state("sebastien")
    assert google._verify_state(state) == "sebastien"
    # Forgery: altering the signed payload without re-signing must fail.
    expiry, _sub, sig = base64.urlsafe_b64decode(state.encode()).decode().split(".", 2)
    forged = base64.urlsafe_b64encode(f"{expiry}.attacker.{sig}".encode()).decode()
    assert google._verify_state(forged) is None
    assert google._verify_state("garbage") is None


def test_enrolment_flow_end_to_end(data_dir, monkeypatch):
    """Browser flow: forwardAuth header -> Google consent redirect -> callback
    stores the per-user refresh token. JWT-exempt but SSO-guarded paths."""
    from urllib.parse import parse_qs, urlparse

    from starlette.testclient import TestClient

    from rosetta.main import create_app

    monkeypatch.setenv("ROSETTA_AUTH", "oidc")  # auth ON: enroll must be open
    app = create_app()
    with TestClient(app) as client:
        # Without the forwardAuth header: refused.
        assert client.get("/google/enroll", follow_redirects=False).status_code == 403
        r = client.get("/google/enroll", headers={"Remote-User": "sebastien"},
                       follow_redirects=False)
        assert r.status_code == 302
        target = urlparse(r.headers["location"])
        assert target.hostname == "accounts.google.com"
        query = parse_qs(target.query)
        assert query["access_type"] == ["offline"]
        state = query["state"][0]

        def handler(request):
            assert b"grant_type=authorization_code" in request.read()
            return httpx.Response(200, json={
                "access_token": "at", "refresh_token": "rt-new", "scope": "a b",
            })

        monkeypatch.setattr(google, "_transport", mock(handler))
        r = client.get(f"/google/callback?code=abc&state={state}")
        assert r.status_code == 200 and "sebastien" in r.text
    stored = json.loads((data_dir / "users" / "sebastien.json").read_text())
    assert stored["refresh_token"] == "rt-new"


def test_enrolment_recovers_utf8_mangled_header(data_dir, monkeypatch):
    """HTTP headers travel as latin-1 while Authelia emits UTF-8: the accents
    of « Sébastien » must survive into the stored credential key."""
    from urllib.parse import parse_qs, urlparse

    from starlette.testclient import TestClient

    from rosetta.main import create_app

    monkeypatch.setenv("ROSETTA_AUTH", "oidc")
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/google/enroll",
                       headers=[(b"Remote-User", "Sébastien".encode("utf-8"))],
                       follow_redirects=False)
        assert r.status_code == 302
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]

        def handler(request):
            return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "scope": ""})

        monkeypatch.setattr(google, "_transport", mock(handler))
        r = client.get(f"/google/callback?code=abc&state={state}")
        assert r.status_code == 200 and "Sébastien" in r.text
    stored = json.loads((data_dir / "users" / "S_bastien.json").read_text())
    assert stored["sub"] == "Sébastien"


def test_calendar_create_all_day_vs_datetime(enrolled, monkeypatch):
    captured = {}

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        captured.update(json.loads(request.read()))
        return httpx.Response(200, json={"id": "ev1", "htmlLink": "https://cal"})

    monkeypatch.setattr(google, "_transport", mock(handler))
    out = run(google.calendar_create("Vacances", "2026-08-01", "2026-08-15"))
    assert out["id"] == "ev1"
    assert captured["start"] == {"date": "2026-08-01"}
    run(google.calendar_create("Dîner", "2026-08-01T20:00:00+02:00", "2026-08-01T22:00:00+02:00"))
    assert "dateTime" in captured["start"]
