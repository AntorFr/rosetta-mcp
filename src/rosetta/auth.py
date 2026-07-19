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

import os
from dataclasses import dataclass

import jwt
from starlette.responses import JSONResponse

# Paths served without a token: health probes and OAuth discovery documents.
_EXEMPT_PREFIXES = ("/health", "/.well-known/")

_ALGORITHMS = ["RS256", "PS256", "ES256"]


@dataclass(frozen=True)
class AuthConfig:
    enabled: bool
    issuer: str
    audience: str
    external_url: str
    jwks_uri: str

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
        if path.startswith(_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        auth = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                auth = value.decode("latin-1")
                break
        scheme, _, token = auth.partition(" ")

        error = None
        if scheme.lower() != "bearer" or not token.strip():
            error = "missing bearer token"
        else:
            try:
                claims = self._decode(token.strip())
            except Exception as exc:  # signature, issuer, audience, expiry...
                error = f"invalid token: {type(exc).__name__}"
            else:
                # Expose claims to downstream apps (future per-addon authorization).
                scope.setdefault("state", {})["token_claims"] = claims

        if error is not None:
            response = JSONResponse(
                {"error": "invalid_token", "error_description": error},
                status_code=401,
                headers={
                    # RFC 9728 §5.1: point OAuth-aware clients at our metadata.
                    "WWW-Authenticate": (
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
