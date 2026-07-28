"""Rosetta - a modular MCP hub.

One HTTP service hosting many thin MCP servers (addons), each mounted under its
own path, behind a single OIDC bearer-token authentication layer (Authelia).
"""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: pyproject. Hardcoding it here silently drifted for
    # three releases, so /health reported 0.2.4 while serving 0.4.0 - the very
    # number one reads to check a deploy landed.
    #
    # This reads the INSTALLED distribution metadata, so the image (a clean build
    # per release) is always exact; a dev venv installed once and never
    # reinstalled will lag until `pip install -e .` is re-run. That beats the
    # hardcoded string, which lagged in production too.
    __version__ = version("rosetta-mcp")
except PackageNotFoundError:  # source tree, not installed at all
    __version__ = "0+unknown"
