"""Shared helpers for addons. Underscore prefix = never mounted by the loader."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import unicodedata

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import HTMLResponse

from ..auth import current_claims

logger = logging.getLogger("rosetta.common")

TIMEOUT = 15.0


def http_client(transport, timeout=TIMEOUT, **kw) -> httpx.AsyncClient:
    """The one place an addon's `httpx.AsyncClient` is built.

    `transport` is the addon's own test hook (`addon._transport`, None in production),
    passed in rather than read here so tests keep patching the addon. Extra keyword
    arguments (headers, follow_redirects) go straight to httpx.
    """
    return httpx.AsyncClient(timeout=timeout, transport=transport, **kw)


def remote_user(request) -> str | None:
    """The SSO subject behind a browser-facing enrolment route, or None.

    Set by the Authelia forwardAuth in front of those paths (ingress-level).

    ⚠️ HTTP headers are latin-1 on the wire but Authelia emits UTF-8 bytes, so an
    accented name arrives mangled ("SÃ©bastien"). Recovering it is NOT cosmetic:
    this value becomes the KEY of the per-user credential store, while tool calls
    look that store up with `preferred_username` taken from the JWT - which is
    JSON, hence properly decoded. Skip the recovery and enrolment silently files
    the credential under a key no call will ever match.

    Lives here, shared, because it was solved once in `google` then re-typed
    wrong in `github` (2026-07-31). A third addon inherits the fix, not the bug.
    """
    value = request.headers.get("Remote-User")
    if value is None:
        return None
    try:
        value = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        logger.debug("Remote-User n'est pas du latin-1 recodé, gardé tel quel")
    # NFC: the same name typed on two keyboards must yield the same key.
    return unicodedata.normalize("NFC", value)


def current_subject() -> str | None:
    """The calling user's store key from the verified token claims, or None.

    Authelia access tokens may carry an opaque `sub`, so the `preferred_username`
    claim wins, NFC-normalized to match what enrolment (`remote_user`) filed the
    credential under. One byte of difference here loses an enrolled account.
    """
    claims = current_claims.get()
    if not claims:
        return None
    value = claims.get("preferred_username") or claims.get("sub")
    return unicodedata.normalize("NFC", str(value)) if value else None


# --------------------------------------------------------------------------
# Identity bridge: opaque IdP subject -> the username the store is keyed on
# --------------------------------------------------------------------------
#
# A user-data store is keyed on the USERNAME, because that is all enrolment
# ever sees (the ingress forwardAuth hands the hub `Remote-User`, never the
# IdP's opaque `sub`). But a delegation token minted for the hour-H path
# carries ONLY the opaque subject - no `preferred_username` claim at all - so
# it needs a way back to the username the credential was filed under.
#
# The one place both identities co-occur UNDER A SIGNATURE is a verified
# free-regime token carrying `preferred_username` AND `sub`: the bridge is
# learned there, lazily, on ordinary authenticated calls. That is also what
# makes it self-repairing: an Authelia storage reset mints NEW opaque
# identifiers, and the next call carries the new `sub` next to the same
# username - the pair relearns itself, no migration, no operator gesture.


def _bridge_path(data_dir: str) -> str:
    return os.path.join(data_dir, "identity_bridge.json")


def _bridge_load(data_dir: str) -> dict:
    # Tolerant on purpose: a missing or mangled file is an EMPTY bridge that
    # the next dual-claim call rebuilds - never an outage.
    try:
        with open(_bridge_path(data_dir)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def bridge_learn(data_dir: str, sub: str, username: str) -> None:
    """Record that opaque subject `sub` is the user filed under `username`.

    Call it ONLY with the two claims of one same verified token - that
    signature is what attests the equivalence. Last write wins, deliberately:
    after an IdP storage reset the fresh `sub` must displace nothing but its
    own absence, and a stale entry is harmless (it still names the same user).
    The bridge holds usernames, not credentials - it is not a secret.
    """
    if not sub or not username or sub == username:
        return
    bridge = _bridge_load(data_dir)
    if bridge.get(sub) == username:
        return
    bridge[sub] = username
    os.makedirs(data_dir, exist_ok=True)
    path = _bridge_path(data_dir)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(bridge, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)  # atomic: a reader sees old or new, never half


def bridge_resolve(data_dir: str, sub: str) -> str | None:
    """The username `sub` was last seen next to, or None if never seen."""
    return _bridge_load(data_dir).get(sub)


def new_server(name: str) -> FastMCP:
    """A FastMCP configured for hub mounting: stateless streamable HTTP served
    at the mount root. The SDK's DNS-rebinding protection is disabled: it only
    fits localhost servers, and would 421 any request carrying the public Host
    header - the hub's own JWT layer is the actual protection."""
    return FastMCP(
        name,
        streamable_http_path="/",
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


def enrol_page(service: str, glyph: str, title: str, message: str,
               status: int = 200, extra: str = "") -> HTMLResponse:
    """Minimal self-contained page for a browser-facing enrolment flow. Shared so
    every user-data addon greets the user with the same card, whatever it
    enrols.

    `extra` is raw HTML appended inside the card, for the flows that need the
    user to DO something on the page rather than be redirected - a form, a
    command to copy. Callers own its content; nothing here is escaped, so it is
    for addon-authored markup only, never for anything a request carried in."""
    return HTMLResponse(f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>rosetta — {service}</title><style>
 body{{margin:0;min-height:100vh;display:grid;place-items:center;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  background:#f4f1ea;color:#2b2b2b}}
 .card{{max-width:26rem;margin:1rem;padding:2.4rem 2.6rem;border-radius:16px;
  background:#fff;box-shadow:0 10px 34px rgba(0,0,0,.09);text-align:center}}
 .glyph{{font-size:2.6rem;line-height:1}}
 h1{{font-size:.82rem;letter-spacing:.22em;text-transform:uppercase;
  opacity:.5;margin:1rem 0 .4rem}}
 h2{{font-size:1.15rem;margin:.2rem 0 .8rem}}
 p{{line-height:1.55;margin:0;opacity:.85}}
 @media (prefers-color-scheme:dark){{
  body{{background:#171614;color:#eae6df}}
  .card{{background:#232019;box-shadow:0 10px 34px rgba(0,0,0,.55)}}}}
</style></head><body><div class="card">
<div class="glyph">{glyph}</div><h1>Rosetta · {service}</h1>
<h2>{title}</h2><p>{message}</p>{extra}
</div></body></html>""", status_code=status)


def dig(d, *path, default=None):
    """Walk nested dicts/lists without raising; int keys index into lists."""
    cur = d
    for k in path:
        if isinstance(k, int):
            if not isinstance(cur, list) or not -len(cur) <= k < len(cur):
                return default
            cur = cur[k]
        elif isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return default
        if cur is None:
            return default
    return cur


def read_user(path: str, default=None):
    """A per-subject JSON record, or `default` if the file is absent or unreadable."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_user(path: str, record: dict) -> None:
    """Atomic (`.tmp` + chmod 600 + replace): the refresh token may rotate, and a
    half-written file is a lost credential - there is no way to re-derive it but
    another browser login."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(record, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# --- `state` OAuth signé (google, withings) -------------------------------
# Format `expiry.sub.sig` en base64 url-safe, digest SHA-256 complet, validité 600 s,
# clé `state.key` (0600) sous le répertoire de données. github garde son propre
# `state` (format `sub|ts`, digest tronqué, clé distincte) : l'unifier invaliderait
# les états en vol.

STATE_TTL = 600


def state_key(data_dir: str) -> bytes:
    path = os.path.join(data_dir, "state.key")
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        os.makedirs(data_dir, exist_ok=True)
        with open(path, "wb") as f:
            f.write(key)
        os.chmod(path, 0o600)
        return key


def sign_state(sub: str, key: bytes) -> str:
    payload = f"{int(time.time()) + STATE_TTL}.{sub}"
    sig = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()


def verify_state(state: str, key: bytes) -> str | None:
    try:
        expiry, sub, sig = base64.urlsafe_b64decode(state.encode()).decode().split(".", 2)
        payload = f"{expiry}.{sub}"
        if not hmac.compare_digest(sig, hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()):
            return None
        if time.time() > int(expiry):
            return None
        return sub
    except Exception:
        return None


def state_codec(data_dir):
    """`(sign, verify)` d'un addon ; `data_dir` est appelé à chaque usage (l'addon
    garde son propre `_data_dir`, que les tests redirigent)."""
    return (
        lambda sub: sign_state(sub, state_key(data_dir())),
        lambda state: verify_state(state, state_key(data_dir())),
    )
