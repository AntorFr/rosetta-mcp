"""User-identity plumbing: machine tokens rejected on user addons, and the
caller's `sub` reaches the tool code through the stateless MCP stack."""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

import fake_addons
from rosetta import auth as auth_module
from rosetta.main import create_app

ISSUER = "https://issuer.test"
AUDIENCE = "https://rosetta.test"

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def app(monkeypatch, rsa_key):
    monkeypatch.setenv("ROSETTA_AUTH", "oidc")
    monkeypatch.setenv("ROSETTA_ISSUER", ISSUER)
    monkeypatch.setenv("ROSETTA_EXTERNAL_URL", AUDIENCE)
    monkeypatch.delenv("ROSETTA_AUDIENCE", raising=False)
    monkeypatch.delenv("ROSETTA_ADDONS", raising=False)
    monkeypatch.setattr(
        auth_module.BearerJWTMiddleware,
        "_signing_key",
        lambda self, token: rsa_key.public_key(),
    )
    return create_app(addons_package=fake_addons)


def sign(rsa_key, sub: str) -> str:
    return jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "iat": int(time.time()),
         "exp": int(time.time()) + 300, "sub": sub},
        rsa_key, algorithm="RS256",
    )


def call_tool(client, path, token, name, args=None):
    return client.post(
        path,
        headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "id": 7, "method": "tools/call",
              "params": {"name": name, "arguments": args or {}}},
    )


def test_machine_token_rejected_on_user_addon(app, rsa_key):
    token = sign(rsa_key, "oauth2:client:agent-alfred")
    with TestClient(app) as client:
        r = client.post("/whoami/", headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_machine_token_accepted_on_machine_addon(app, rsa_key):
    token = sign(rsa_key, "oauth2:client:agent-alfred")
    with TestClient(app) as client:
        r = call_tool(client, "/ok/", token, "ping")
    assert r.status_code == 200
    assert "pong" in r.text


def test_user_sub_reaches_the_tool(app, rsa_key):
    """The load-bearing test: claims set by the middleware are visible from
    inside a tool executed by the stateless streamable-HTTP stack."""
    token = sign(rsa_key, "sebastien")
    with TestClient(app) as client:
        r = call_tool(client, "/whoami/", token, "whoami")
    assert r.status_code == 200
    assert "sebastien" in r.text
