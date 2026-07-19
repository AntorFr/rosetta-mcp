"""Rosetta hub: discovers addons, mounts each one under /<name>, serves /health.

Addon contract (a module inside the addons package):
  - module-level `mcp` = a FastMCP instance built with
    `streamable_http_path="/"` and `stateless_http=True`;
  - optional `required_env: list[str]` - keys whose absence degrades the addon
    (tools still answer, with their own explicit error messages) without ever
    blocking the hub;
  - optional `identity = "user"` - the addon refuses machine tokens: callers
    must present a token with a human subject (user-data addons);
  - optional `extra_routes: list[(suffix, endpoint, methods)]` - plain HTTP
    routes registered at /<name><suffix> (e.g. OAuth enrolment pages), and
    `open_paths: list[suffix]` - those of them exempt from the hub JWT check
    (they are browser-facing; the ingress forwardAuth guards them instead);
  - module names starting with `_` are ignored (shared helpers live there).

Isolation is the design rule: an addon that fails to import (or to start) is
reported on /health and skipped - the hub and the other addons stay up.
"""

from __future__ import annotations

import dataclasses
import importlib
import logging
import os
import pkgutil
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from . import __version__
from . import addons as default_addons_package
from .auth import AuthConfig, BearerJWTMiddleware, protected_resource_metadata

logger = logging.getLogger("rosetta")


@dataclass
class Addon:
    name: str
    state: str  # "ok" | "degraded" | "error" | "disabled"
    detail: str | None = None
    mcp: object | None = None
    identity: str = "machine"  # "user" = machine tokens refused
    extra_routes: list = field(default_factory=list)
    open_paths: list = field(default_factory=list)

    @property
    def mounted(self) -> bool:
        return self.state in ("ok", "degraded")


def discover_addons(package=None, enabled: set[str] | None = None) -> list[Addon]:
    """Import every addon module, converting individual failures into statuses."""
    package = package or default_addons_package
    found: list[Addon] = []
    for info in pkgutil.iter_modules(package.__path__):
        name = info.name
        if name.startswith("_"):
            continue
        if enabled is not None and name not in enabled:
            found.append(Addon(name, "disabled", "not in ROSETTA_ADDONS"))
            continue
        try:
            # Fresh execution on every discovery: a FastMCP session manager can
            # only be started once, so a second create_app() in the same
            # process (tests) must get new instances, not cached modules.
            full = f"{package.__name__}.{name}"
            if full in sys.modules:
                module = importlib.reload(sys.modules[full])
            else:
                module = importlib.import_module(full)
        except Exception as exc:
            logger.exception("addon %s failed to import", name)
            found.append(Addon(name, "error", f"import failed: {type(exc).__name__}: {exc}"))
            continue
        server = getattr(module, "mcp", None)
        if server is None:
            found.append(Addon(name, "error", "module exposes no `mcp` FastMCP instance"))
            continue
        missing = [k for k in getattr(module, "required_env", []) if not os.environ.get(k)]
        state, detail = ("degraded", f"missing env: {', '.join(missing)}") if missing else ("ok", None)
        found.append(Addon(
            name, state, detail, server,
            identity=getattr(module, "identity", "machine"),
            extra_routes=list(getattr(module, "extra_routes", [])),
            open_paths=list(getattr(module, "open_paths", [])),
        ))
    return found


def create_app(addons_package=None) -> Starlette:
    auth_config = AuthConfig.from_env()
    enabled_env = os.environ.get("ROSETTA_ADDONS", "").strip()
    enabled = {n.strip() for n in enabled_env.split(",") if n.strip()} if enabled_env else None
    addons = discover_addons(addons_package, enabled)

    mounts, addon_routes = [], []
    for addon in addons:
        if not addon.mounted:
            continue
        try:
            # Specific routes are registered before the catch-all mount so
            # /<name>/enroll wins over the MCP endpoint at /<name>/.
            for suffix, endpoint, methods in addon.extra_routes:
                addon_routes.append(Route(f"/{addon.name}{suffix}", endpoint, methods=methods))
            mounts.append(Mount(f"/{addon.name}", app=addon.mcp.streamable_http_app()))
        except Exception as exc:
            logger.exception("addon %s failed to build its HTTP app", addon.name)
            addon.state, addon.detail = "error", f"app build failed: {type(exc).__name__}: {exc}"

    auth_config = dataclasses.replace(
        auth_config,
        user_only_prefixes=tuple(f"/{a.name}" for a in addons if a.mounted and a.identity == "user"),
        open_prefixes=tuple(f"/{a.name}{p}" for a in addons if a.mounted for p in a.open_paths),
    )

    @asynccontextmanager
    async def lifespan(app: Starlette):
        # Each addon's session manager is started independently: a failure
        # downgrades that addon instead of aborting the hub startup.
        async with AsyncExitStack() as stack:
            for addon in addons:
                if not addon.mounted:
                    continue
                try:
                    await stack.enter_async_context(addon.mcp.session_manager.run())
                except Exception as exc:
                    logger.exception("addon %s failed to start", addon.name)
                    addon.state, addon.detail = "error", f"start failed: {type(exc).__name__}: {exc}"
            yield

    def addon_report() -> dict:
        return {
            a.name: ({"state": a.state, "detail": a.detail} if a.detail else {"state": a.state})
            for a in addons
        }

    async def health(request: Request) -> JSONResponse:
        # Unauthenticated: k8s probes + humans checking partial degradation.
        states = {a.state for a in addons}
        status = "ok" if states <= {"ok", "disabled"} else "degraded"
        return JSONResponse({"status": status, "version": __version__, "addons": addon_report()})

    async def index(request: Request) -> JSONResponse:
        return JSONResponse({"service": "rosetta", "version": __version__, "addons": addon_report()})

    async def resource_metadata(request: Request) -> JSONResponse:
        addon = request.path_params.get("addon")
        return JSONResponse(protected_resource_metadata(auth_config, addon))

    routes = [
        Route("/health", health),
        Route("/", index),
        Route("/.well-known/oauth-protected-resource", resource_metadata),
        Route("/.well-known/oauth-protected-resource/{addon}", resource_metadata),
        *addon_routes,
        *mounts,
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    return BearerJWTMiddleware(app, auth_config)


app = create_app()
