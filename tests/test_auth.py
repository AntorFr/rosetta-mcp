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


def test_git_paths_challenge_in_basic_or_no_helper_is_ever_called(app):
    """Under /git the client is git, and git cannot read a Bearer challenge.

    Faced with one it gives up on "Authentication failed" WITHOUT asking its
    credential helper — so a valid token never gets a chance, and the helper
    meant to carry the channel and the shield could never run (measured
    2026-08-10). Everywhere else the RFC 9728 pointer stays untouched.
    """
    with TestClient(app) as client:
        git = client.get("/git/AntorFr/x/info/refs?service=git-upload-pack")
        mcp = client.post("/ok/")
    assert git.status_code == 401
    assert git.headers["WWW-Authenticate"].startswith("Basic ")
    assert "oauth-protected-resource" in mcp.headers["WWW-Authenticate"]


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


# --- Basic as an envelope: git cannot send a Bearer header (rosetta >= 0.14.0) ---

def basic(token: str, user: str = "x-access-token") -> str:
    import base64
    return "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()


def test_basic_carrying_the_same_jwt_passes(app, rsa_key):
    with TestClient(app) as client:
        r = client.get("/", headers={"Authorization": basic(sign(rsa_key))})
    assert r.status_code == 200


def test_basic_is_an_envelope_not_a_bypass(app, rsa_key):
    # Same envelope, expired token: still refused. Basic widens how the token
    # travels, never what is trusted.
    token = sign(rsa_key, exp=int(time.time()) - 10)
    with TestClient(app) as client:
        r = client.get("/", headers={"Authorization": basic(token)})
    assert r.status_code == 401


def test_basic_without_a_password_is_refused(app):
    import base64
    header = "Basic " + base64.b64encode(b"someone-without-a-password").decode()
    with TestClient(app) as client:
        r = client.get("/", headers={"Authorization": header})
    assert r.status_code == 401


def test_unparsable_basic_is_refused_not_crashed(app):
    with TestClient(app) as client:
        r = client.get("/", headers={"Authorization": "Basic not-base64!!"})
    assert r.status_code == 401


def test_token_from_header_ignores_unknown_schemes():
    assert auth_module.token_from_header("Digest abc") == ""
    assert auth_module.token_from_header("") == ""
