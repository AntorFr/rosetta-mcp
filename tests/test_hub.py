"""Loader isolation + health + MCP roundtrip, auth disabled."""

import json

from starlette.testclient import TestClient

import fake_addons
from rosetta.main import create_app

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


def make_app(monkeypatch, **env):
    monkeypatch.setenv("ROSETTA_AUTH", "off")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return create_app(addons_package=fake_addons)


def test_broken_addon_does_not_take_hub_down(monkeypatch):
    app = make_app(monkeypatch)
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert body["addons"]["broken"]["state"] == "error"
    assert body["addons"]["ok"]["state"] == "ok"
    assert body["addons"]["needy"]["state"] == "degraded"
    assert "NEEDY_MISSING_KEY" in body["addons"]["needy"]["detail"]


def test_addons_allowlist(monkeypatch):
    app = make_app(monkeypatch, ROSETTA_ADDONS="ok")
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["addons"]["ok"]["state"] == "ok"
    assert body["addons"]["broken"]["state"] == "disabled"


def test_mcp_initialize_roundtrip(monkeypatch):
    app = make_app(monkeypatch)
    with TestClient(app) as client:
        r = client.post("/ok/", json=INITIALIZE, headers=MCP_HEADERS)
    assert r.status_code == 200
    assert '"serverInfo"' in r.text and '"ok"' in r.text


def test_degraded_addon_still_answers(monkeypatch):
    app = make_app(monkeypatch)
    with TestClient(app) as client:
        r = client.post("/needy/", json=INITIALIZE, headers=MCP_HEADERS)
    assert r.status_code == 200


def test_real_addons_discovery(monkeypatch):
    """The shipped package loads: maps + transit mounted, helpers ignored."""
    monkeypatch.setenv("ROSETTA_AUTH", "off")
    app = create_app()
    with TestClient(app) as client:
        body = client.get("/health").json()
    # No API keys in the test env: both addons load as degraded, never error.
    assert body["addons"]["maps"]["state"] == "degraded"
    assert body["addons"]["transit"]["state"] == "degraded"
    assert "_common" not in body["addons"]
