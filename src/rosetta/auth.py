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

# EdDSA is here for the delegation tokens Tessera Control mints: it signs with
# the same Ed25519 key that signs its config bundles, deliberately, so that
# standing up the hour-H path introduced no new secret anywhere.
_ALGORITHMS = ["RS256", "PS256", "ES256", "EdDSA"]

# Authelia issues client_credentials tokens with this subject prefix - the
# discriminant between machine identities and humans.
MACHINE_SUB_PREFIX = "oauth2:client:"

# Claims of the token authenticating the CURRENT request, for addon tools that
# need the caller's identity (user-data addons key their credential store on
# `sub`). Set by the middleware; stateless HTTP keeps handling in-task, which a
# dedicated test asserts.
current_claims: ContextVar[dict | None] = ContextVar("rosetta_claims", default=None)

# The RAW bearer of the current request, for addons that EXCHANGE the caller's
# token against a downstream credential (mail: OpenBao JWT login, where the
# vault's templated policy — not addon code — decides what this identity may
# read). Decoded claims cannot be replayed; the exchange needs the signed
# token itself. Same lifecycle as `current_claims`.
current_token: ContextVar[str | None] = ContextVar("rosetta_token", default=None)


def token_from_header(value: str) -> str:
    """The access token carried by an `Authorization` header, or "".

    Bearer is the normal envelope. Basic is accepted too, taking the PASSWORD as
    the token, because **git cannot be taught to send a Bearer header**: a
    credential helper hands it a username and a password, nothing else. This is
    GitHub's own `x-access-token:<token>` convention, and it widens the envelope
    only - the token inside is validated exactly like a Bearer one, by the same
    signature, issuer and audience checks.

    A Basic *challenge* is emitted only under `_BASIC_CHALLENGE_PREFIXES` (i.e.
    `/git/`, whose client is git and never a browser). Everywhere else the 401
    still points at the RFC 9728 metadata, so no browser is invited to prompt.
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
    # (issuer, jwks_uri) pairs this hub accepts tokens from, the IdP first.
    #
    # A second issuer is NOT a second authorization server: Tessera Control
    # mints hour-H delegation tokens, and no client can run a flow against it.
    # It is therefore trusted for VALIDATION and never advertised in the RFC
    # 9728 document - pointing a client at a place it cannot get a token is
    # worse than saying nothing.
    trusted_issuers: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_env(cls) -> "AuthConfig":
        # AUCUN DÉFAUT sur ces deux-là, et c'est délibéré : ce dépôt est PUBLIC.
        # Ils valaient l'émetteur et l'URL de MON déploiement — donc mon DNS livré
        # dans une image que d'autres peuvent tirer, et inutilisable par eux. Ces
        # valeurs se déclarent dans le manifeste du hub, où elles ont leur place.
        issuer = os.environ.get("ROSETTA_ISSUER", "").rstrip("/")
        external = os.environ.get("ROSETTA_EXTERNAL_URL", "").rstrip("/")
        return cls(
            # "off" is meant for local development only.
            enabled=os.environ.get("ROSETTA_AUTH", "oidc").lower() != "off",
            issuer=issuer,
            audience=os.environ.get("ROSETTA_AUDIENCE", external),
            external_url=external,
            # Authelia serves its JWKS at /jwks.json; override if the IdP differs.
            jwks_uri=os.environ.get("ROSETTA_JWKS_URI", f"{issuer}/jwks.json"),
            trusted_issuers=_trusted_issuers(
                issuer,
                os.environ.get("ROSETTA_JWKS_URI", f"{issuer}/jwks.json"),
                os.environ.get("ROSETTA_TRUSTED_ISSUERS", ""),
            ),
        )

    def audiences_for(self, path: str) -> list[str]:
        """The `aud` values acceptable on this path.

        TWO, and the pair is the whole point. The hub identifier is accepted
        everywhere - that is what the IdP issues and what every existing token
        carries. The MOUNT identifier is accepted only on its own mount.

        So a token that knows which addon it was for is held to it, while one
        that only knows the hub keeps working exactly as before. Tessera's
        delegation tokens are of the first kind: the envelope a human signed
        named one logical server, and this is where that precision stops being
        thrown away at the door.
        """
        auds = [self.audience]
        addon = path.strip("/").split("/", 1)[0]
        if addon:
            auds.append(f"{self.audience}/{addon}")
        return auds


def _trusted_issuers(issuer: str, jwks_uri: str, extra: str) -> tuple[tuple[str, str], ...]:
    """Parse ROSETTA_TRUSTED_ISSUERS: comma-separated `iss` or `iss=jwks_uri`.

    The default JWKS location differs between the two worlds we actually face
    - Authelia serves `/jwks.json`, Tessera Control serves the RFC 8414 path -
    so an entry may name its own, and otherwise `/.well-known/jwks.json` is
    assumed because that is the standard one.
    """
    out: list[tuple[str, str]] = [(issuer, jwks_uri)] if issuer else []
    seen = {issuer}
    for item in extra.split(","):
        item = item.strip()
        if not item:
            continue
        iss, _, uri = item.partition("=")
        iss = iss.strip().rstrip("/")
        if not iss or iss in seen:
            continue
        seen.add(iss)
        out.append((iss, uri.strip() or f"{iss}/.well-known/jwks.json"))
    return tuple(out)


class BearerJWTMiddleware:
    """Pure ASGI middleware: rejects any non-exempt request without a valid JWT."""

    def __init__(self, app, config: AuthConfig):
        self.app = app
        self.config = config
        self._jwks_clients: dict[str, jwt.PyJWKClient] = {}

    def _claimed_issuer(self, token: str) -> str:
        """The `iss` the token CLAIMS, read without verifying anything.

        Reading an unverified claim is safe here and only here: it selects
        which key set to verify against, and a token naming an issuer we do
        not trust is refused before any key is fetched. Nothing is believed -
        the signature check that follows is what decides.
        """
        try:
            claimed = jwt.decode(token, options={"verify_signature": False}).get("iss", "")
        except Exception:
            return ""
        claimed = str(claimed).rstrip("/")
        for issuer, _ in self.config.trusted_issuers:
            if issuer == claimed:
                return issuer
        return ""

    def _signing_key(self, token: str):
        # Lazy and PER ISSUER: a key set is only fetched on the first request
        # that claims that issuer, so the hub boots (and /health answers) even
        # if an IdP is down - and adding a second trusted issuer costs nothing
        # until somebody actually presents a token from it.
        issuer = self._claimed_issuer(token)
        if not issuer:
            raise ValueError("token issuer is not trusted by this resource")
        client = self._jwks_clients.get(issuer)
        if client is None:
            uri = next(u for i, u in self.config.trusted_issuers if i == issuer)
            client = jwt.PyJWKClient(uri, cache_keys=True)
            self._jwks_clients[issuer] = client
        return client.get_signing_key_from_jwt(token).key

    def _decode(self, token: str, audiences: list[str] | None = None) -> dict:
        issuer = self._claimed_issuer(token) or self.config.issuer
        return jwt.decode(
            token,
            self._signing_key(token),
            algorithms=_ALGORITHMS,
            audience=audiences if audiences is not None else self.config.audience,
            issuer=issuer,
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
                claims = self._decode(token, self.config.audiences_for(path))
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
                    # Expose claims and the raw token to downstream addon tools.
                    scope.setdefault("state", {})["token_claims"] = claims
                    current_claims.set(claims)
                    current_token.set(token)

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
