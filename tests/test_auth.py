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


# --- The anonymous surface window: catalog observation without a credential
#
# The Tessera Guard gate polls initialize + tools/list to watch the tool
# surface for drift, and deliberately holds no hub credential of its own.
# So the SURFACE methods are served without a token - and only them, on the
# mount roots, for ONE well-formed JSON-RPC object. One test per edge.

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "observer", "version": "0"}},
}

TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


def test_surface_is_served_anonymously_on_user_and_machine_mounts(app):
    # whoami is identity="user", ok is machine class: the window ignores the
    # distinction, because listing runs no tool body and reads no claims.
    with TestClient(app) as client:
        user = client.post("/whoami/", json=TOOLS_LIST, headers=MCP_HEADERS)
        machine = client.post("/ok/", json=TOOLS_LIST, headers=MCP_HEADERS)
    assert user.status_code == 200 and '"whoami"' in user.text
    assert machine.status_code == 200 and '"ping"' in machine.text


def test_initialize_is_served_anonymously(app):
    with TestClient(app) as client:
        r = client.post("/ok/", json=INITIALIZE, headers=MCP_HEADERS)
    assert r.status_code == 200
    assert '"serverInfo"' in r.text


def test_tools_call_without_a_token_stays_401_with_discovery(app):
    call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "ping", "arguments": {}}}
    with TestClient(app) as client:
        r = client.post("/ok/", json=call, headers=MCP_HEADERS)
    assert r.status_code == 401
    assert "oauth-protected-resource" in r.headers["WWW-Authenticate"]


def test_an_unreadable_body_stays_401(app):
    with TestClient(app) as client:
        r = client.post("/ok/", content=b"\x00not-json", headers=MCP_HEADERS)
    assert r.status_code == 401


def test_a_batch_stays_401_even_all_white(app):
    with TestClient(app) as client:
        r = client.post("/ok/", json=[TOOLS_LIST, INITIALIZE],
                        headers=MCP_HEADERS)
    assert r.status_code == 401


def test_an_invalid_token_on_a_white_method_stays_401(app, rsa_key):
    # A rotten token is not an anonymous caller.
    token = sign(rsa_key, exp=int(time.time()) - 10)
    with TestClient(app) as client:
        r = client.post("/ok/", json=TOOLS_LIST,
                        headers=MCP_HEADERS | {"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_the_sse_get_stays_authenticated(app):
    with TestClient(app) as client:
        r = client.get("/ok/", headers={"Accept": "text/event-stream"})
    assert r.status_code == 401


def test_git_posts_never_pass_the_window(app):
    # Two path segments: the bare-HTTP proxy, which relays upstream WITH the
    # hub's credential, is outside the window's shape by construction.
    with TestClient(app) as client:
        r = client.post("/git/AntorFr/x/git-upload-pack", json=TOOLS_LIST,
                        headers=MCP_HEADERS)
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"].startswith("Basic ")


# --- Tessera delegation tokens: a second trusted issuer, EdDSA, per-mount aud
#
# Tessera Control mints an hour-H token when a human signed for the call in
# advance. It is NOT a second authorization server - no client can run a flow
# against it - so the hub trusts it for VALIDATION only, and never advertises
# it. Two things had to change for those tokens to be verifiable at all: the
# algorithm (Control signs EdDSA, with the same key that signs its config
# bundles, so that standing up hour H introduced no new secret), and the
# audience, which names ONE MOUNT rather than the hub.

DELEGATION_ISSUER = "https://control.test"


@pytest.fixture
def ed_key():
    from cryptography.hazmat.primitives.asymmetric import ed25519

    return ed25519.Ed25519PrivateKey.generate()


@pytest.fixture
def hub(monkeypatch, rsa_key, ed_key):
    """The hub, trusting the IdP and Tessera Control, each with its own key."""
    monkeypatch.setenv("ROSETTA_AUTH", "oidc")
    monkeypatch.setenv("ROSETTA_ISSUER", ISSUER)
    monkeypatch.setenv("ROSETTA_EXTERNAL_URL", AUDIENCE)
    monkeypatch.setenv("ROSETTA_TRUSTED_ISSUERS", DELEGATION_ISSUER)
    monkeypatch.delenv("ROSETTA_AUDIENCE", raising=False)
    monkeypatch.delenv("ROSETTA_ADDONS", raising=False)

    def signing_key(self, token):
        claimed = jwt.decode(token, options={"verify_signature": False}).get("iss")
        if claimed == DELEGATION_ISSUER:
            return ed_key.public_key()
        return rsa_key.public_key()

    monkeypatch.setattr(auth_module.BearerJWTMiddleware, "_signing_key", signing_key)
    return create_app(addons_package=fake_addons)


def delegation(ed_key, audience: str, **overrides) -> str:
    claims = {
        "iss": DELEGATION_ISSUER,
        "aud": audience,
        "sub": "sebastien",
        "act": "skippy",
        "grant": "g_test",
        "iat": int(time.time()),
        "exp": int(time.time()) + 120,
    }
    claims.update(overrides)
    return jwt.encode(claims, ed_key, algorithm="EdDSA")


def test_delegation_is_accepted_on_the_mount_it_names(hub, ed_key):
    token = delegation(ed_key, f"{AUDIENCE}/ok")
    with TestClient(hub) as client:
        r = client.post("/ok/", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code != 401, r.text


def test_delegation_for_one_mount_is_refused_on_another(hub, ed_key):
    """The whole point of decision B: precision survives to the resource.

    A human signed an envelope naming one logical server. The token says which
    mount it was for, and this hub is a single process serving many - so the
    audience is the only thing standing between "you may tag a repo" and "you
    may send mail as me".
    """
    token = delegation(ed_key, f"{AUDIENCE}/whoami")
    with TestClient(hub) as client:
        r = client.post("/ok/", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_a_hub_wide_token_still_works_everywhere(hub, rsa_key):
    """No regression: what the IdP issues is audienced to the hub, as before."""
    token = sign(rsa_key)
    with TestClient(hub) as client:
        r = client.post("/ok/", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code != 401, r.text


def test_an_untrusted_issuer_is_refused(hub, ed_key):
    token = delegation(ed_key, f"{AUDIENCE}/ok", iss="https://elsewhere.test")
    with TestClient(hub) as client:
        r = client.post("/ok/", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_the_delegation_issuer_is_not_advertised(hub):
    """A client cannot obtain a token there, so pointing it there would lie."""
    with TestClient(hub) as client:
        r = client.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    assert r.json()["authorization_servers"] == [ISSUER]


def test_a_delegation_carries_a_human_not_a_machine(hub, ed_key):
    """User-data addons refuse machine identities; a delegation is not one.

    That is the point of the whole chain - the token carries the `sub` of the
    human who signed, which is exactly the key a user-data addon indexes its
    credential store by.
    """
    token = delegation(ed_key, f"{AUDIENCE}/whoami")
    with TestClient(hub) as client:
        r = client.post("/whoami/", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code != 403, r.text
