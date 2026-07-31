"""Shared helpers for addons. Underscore prefix = never mounted by the loader."""

from __future__ import annotations

import unicodedata

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import HTMLResponse

TIMEOUT = 15.0


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
        pass
    # NFC: the same name typed on two keyboards must yield the same key.
    return unicodedata.normalize("NFC", value)


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
               status: int = 200) -> HTMLResponse:
    """Minimal self-contained page for a browser-facing enrolment flow. Shared so
    every user-data addon greets the user with the same card, whatever it
    enrols."""
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
<h2>{title}</h2><p>{message}</p>
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
