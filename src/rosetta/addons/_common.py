"""Shared helpers for addons. Underscore prefix = never mounted by the loader."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

TIMEOUT = 15.0


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
