"""Bearer JWT enforcement: 401 semantics, RFC 9728 discovery, valid tokens."""

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
    # Short-circuit the JWKS fetch: trust the test key pair.
    monkeypatch.setattr(
        auth_module.BearerJWTMiddleware,
        "_signing_key",
        lambda self, token: rsa_key.public_key(),
    )
    return create_app(addons_package=fake_addons)


def sign(rsa_key, **overrides) -> str:
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
        "sub": "test-agent",
    }
    claims.update(overrides)
    return jwt.encode(claims, rsa_key, algorithm="RS256")


def test_no_token_is_401_with_discovery_pointer(app):
    with TestClient(app) as client:
        r = client.post("/ok/")
    assert r.status_code == 401
    assert "oauth-protected-resource" in r.headers["WWW-Authenticate"]


def test_health_and_wellknown_are_open(app):
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        meta = client.get("/.well-known/oauth-protected-resource").json()
        assert meta["authorization_servers"] == [ISSUER]
        sub = client.get("/.well-known/oauth-protected-resource/ok").json()
        assert sub["resource"] == f"{AUDIENCE}/ok"


def test_valid_token_passes(app, rsa_key):
    with TestClient(app) as client:
        r = client.get("/", headers={"Authorization": f"Bearer {sign(rsa_key)}"})
    assert r.status_code == 200
    assert r.json()["service"] == "rosetta"


def test_wrong_audience_is_401(app, rsa_key):
    token = sign(rsa_key, aud="https://somewhere.else")
    with TestClient(app) as client:
        r = client.get("/", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_expired_token_is_401(app, rsa_key):
    token = sign(rsa_key, exp=int(time.time()) - 10)
    with TestClient(app) as client:
        r = client.get("/", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
