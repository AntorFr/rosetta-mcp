# rosetta - modular MCP hub: thin read-only MCP servers (addons) mounted under
# one HTTP endpoint, behind one OIDC bearer-token auth layer (Authelia).
# API keys live here (server side); agents only ever hold an access token.

# --- Build stage: install the package into a self-contained venv ---
FROM python:3.12-slim AS build

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir .

# --- Runtime ---
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    PATH="/opt/venv/bin:$PATH"

COPY --from=build /opt/venv /opt/venv

RUN useradd --create-home --uid 1000 rosetta
USER rosetta

EXPOSE 8200

# /health is unauthenticated by design (also used by k8s probes).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8200/health', timeout=3).status == 200 else 1)"]

CMD ["uvicorn", "rosetta.main:app", "--host", "0.0.0.0", "--port", "8200"]
