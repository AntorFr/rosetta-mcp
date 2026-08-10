"""Bearer-token authentication for the hub (OAuth 2.1 resource server).

The hub itself holds no credentials and no user database: it validates JWT
access tokens (RFC 9068) issued by an external OIDC provider (Authelia), using
the provider's JWKS. Any grant is accepted as long as the token is valid:
`client_credentials` for machine agents, `authorization_code` + refresh for
user-delegated access.

RFC 9728 (protected resource metadata) is served on the well-known paths so
that OAuth-aware MCP clients can discover the authorization server on their
own, and every 401 carries the `WWW-Authenticate` header pointing to it.
"""

from __future__ import annotations

import base64
import os
from contextvars import ContextVar
from dataclasses import dataclass

import jwt
from starlette.responses import JSONResponse

# Paths served without a token: health probes and OAuth discovery documents.
_EXEMPT_PREFIXES = ("/health", "/.well-known/")

# Paths whose 401 must challenge in **Basic**, because their client is git.
#
# Git does not understand a `Bearer` challenge: faced with one it gives up on
# "Authentication failed" WITHOUT EVER ASKING ITS CREDENTIAL HELPER — so a pod
# holding a perfectly valid token could not push, and no helper could ever be
# written (measured 2026-08-10). This is the narrowest fix: the challenge changes
# only under /git, where the caller is git and never a browser.
_BASIC_CHALLENGE_PREFIXES = ("/git/",)

_ALGORITHMS = ["RS256", "PS256", "ES256"]

# Authelia issues client_credentials tokens with this subject prefix - the
# discriminant between machine identities and humans.
MACHINE_SUB_PREFIX = "oauth2:client:"

# Claims of the token authenticating the CURRENT request, for addon tools that
# need the caller's identity (user-data addons key their credential store on
# `sub`). Set by the middleware; stateless HTTP keeps handling in-task, which a
# dedicated test asserts.
current_claims: ContextVar[dict | None] = ContextVar("rosetta_claims", default=None)


def token_from_header(value: str) -> str:
    """The access token carried by an `Authorization` header, or "".

    Bearer is the normal envelope. Basic is accepted too, taking the PASSWORD as
    the token, because **git cannot be taught to send a Bearer header**: a
    credential helper hands it a username and a password, nothing else. This is
    GitHub's own `x-access-token:<token>` convention, and it widens the envelope
    only - the token inside is validated exactly like a Bearer one, by the same
    signature, issuer and audience checks. No `WWW-Authenticate: Basic` is ever
    emitted, so no browser is invited to prompt for one.
    """
    scheme, _, credential = value.partition(" ")
    scheme = scheme.lower()
    if scheme == "bearer":
        return credential.strip()
    if scheme == "basic":
        try:
            decoded = base64.b64decode(credential.strip(), validate=True).decode("utf-8")
        except Exception:
            return ""
        _, sep, password = decoded.partition(":")
        return password.strip() if sep else ""
    return ""


@dataclass(frozen=True)
class AuthConfig:
    enabled: bool
    issuer: str
    audience: str
    external_url: str
    jwks_uri: str
    # Mount prefixes (e.g. "/google") that refuse machine tokens: the token
    # must carry a HUMAN subject (user-data addons).
    user_only_prefixes: tuple[str, ...] = ()
    # Extra exempt prefixes (browser-facing addon routes such as enrolment
    # callbacks, guarded upstream by the ingress forwardAuth instead).
    open_prefixes: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "AuthConfig":
        issuer = os.environ.get("ROSETTA_ISSUER", "https://auth.berard.me").rstrip("/")
        external = os.environ.get(
            "ROSETTA_EXTERNAL_URL", "https://rosetta.mcp.berard.me"
        ).rstrip("/")
        return cls(
            # "off" is meant for local development only.
            enabled=os.environ.get("ROSETTA_AUTH", "oidc").lower() != "off",
            issuer=issuer,
            audience=os.environ.get("ROSETTA_AUDIENCE", external),
            external_url=external,
            # Authelia serves its JWKS at /jwks.json; override if the IdP differs.
            jwks_uri=os.environ.get("ROSETTA_JWKS_URI", f"{issuer}/jwks.json"),
        )


class BearerJWTMiddleware:
    """Pure ASGI middleware: rejects any non-exempt request without a valid JWT."""

    def __init__(self, app, config: AuthConfig):
        self.app = app
        self.config = config
        self._jwks_client: jwt.PyJWKClient | None = None

    def _signing_key(self, token: str):
        # Lazy: the JWKS is only fetched on the first authenticated request,
        # so the hub boots (and /health answers) even if the IdP is down.
        if self._jwks_client is None:
            self._jwks_client = jwt.PyJWKClient(self.config.jwks_uri, cache_keys=True)
        return self._jwks_client.get_signing_key_from_jwt(token).key

    def _decode(self, token: str) -> dict:
        return jwt.decode(
            token,
            self._signing_key(token),
            algorithms=_ALGORITHMS,
            audience=self.config.audience,
            issuer=self.config.issuer,
            options={"require": ["exp", "iat"]},
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.config.enabled:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith(_EXEMPT_PREFIXES) or path.startswith(self.config.open_prefixes or ()):
            await self.app(scope, receive, send)
            return

        auth = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                auth = value.decode("latin-1")
                break
        token = token_from_header(auth)

        error = None
        status = 401
        if not token:
            error = "missing bearer token"
        else:
            try:
                claims = self._decode(token)
            except Exception as exc:  # signature, issuer, audience, expiry...
                error = f"invalid token: {type(exc).__name__}"
            else:
                sub = str(claims.get("sub", ""))
                if path.startswith(self.config.user_only_prefixes or ()) and (
                    not sub or sub.startswith(MACHINE_SUB_PREFIX)
                ):
                    # User-data addon: a machine identity is not enough.
                    error, status = "this resource requires a user identity token", 403
                else:
                    # Expose claims to downstream apps and addon tools.
                    scope.setdefault("state", {})["token_claims"] = claims
                    current_claims.set(claims)

        if error is not None:
            response = JSONResponse(
                {"error": "invalid_token" if status == 401 else "forbidden",
                 "error_description": error},
                status_code=status,
                headers={
                    # RFC 9728 §5.1: point OAuth-aware clients at our metadata —
                    # except where the client is git, which only speaks Basic.
                    "WWW-Authenticate": (
                        'Basic realm="rosetta"'
                        if path.startswith(_BASIC_CHALLENGE_PREFIXES) else
                        'Bearer resource_metadata='
                        f'"{self.config.external_url}/.well-known/oauth-protected-resource"'
                    )
                },
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def protected_resource_metadata(config: AuthConfig, addon: str | None = None) -> dict:
    """RFC 9728 document, for the hub root or for one addon sub-resource."""
    resource = config.external_url if addon is None else f"{config.external_url}/{addon}"
    return {
        "resource": resource,
        "authorization_servers": [config.issuer],
        "bearer_methods_supported": ["header"],
    }
