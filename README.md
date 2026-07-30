# rosetta-mcp

**Rosetta** is a modular [MCP](https://modelcontextprotocol.io) hub: one HTTP
service hosting many thin, read-only MCP servers (*addons*), each mounted under
its own path, behind a single OIDC bearer-token authentication layer.

Like the stone: one endpoint, and every agent reads it in its own tongue.

```
agent ──Bearer JWT──►  https://rosetta.example.com/maps/      (Google Maps, Places, Weather)
                       https://rosetta.example.com/transit/   (SNCF + IDFM/Navitia)
                       https://rosetta.example.com/<addon>/   (drop a module in, it mounts)
                       https://rosetta.example.com/health     (unauthenticated, per-addon state)
```

Why a hub: provider API keys stay **server-side** (one deployment, one place to
rotate), agents only ever hold an access token, and adding MCP #12 is a new
module + nothing else - no new pod, no new image elsewhere, no key sprayed into
agent environments.

## Addons

An addon is a module in `src/rosetta/addons/` exposing:

```python
from ._common import new_server

mcp = new_server("name")          # FastMCP: stateless streamable HTTP at "/"
required_env = ["SOME_API_KEY"]   # optional
```

The loader mounts each addon under `/<module-name>` and **isolates failures**:

| Addon state | Meaning |
|---|---|
| `ok` | mounted, fully operational |
| `degraded` | mounted, but some `required_env` is missing - tools answer with an explicit error |
| `error` | import/startup failed - addon skipped, **hub and other addons stay up** |
| `disabled` | excluded by `ROSETTA_ADDONS` |

Per-addon state is reported on `GET /health`. Modules starting with `_` are
shared helpers, never mounted. Each addon also runs standalone over stdio for
local debugging (`python -m rosetta.addons.maps`).

### User-data addons (`identity = "user"`)

An addon may declare `identity = "user"`: the hub then refuses machine tokens
on its path (403) - the bearer token must carry a **human** subject. Tools read
the caller's claims via a context variable, so a user-data addon keys its
server-side credential store on `sub`: agents never hold the downstream
credentials, only their own identity token. Such addons may also register plain
HTTP routes (`extra_routes` / `open_paths`) for browser-facing enrolment flows,
guarded by the ingress SSO (forwardAuth) instead of the hub JWT.

Bundled addons: `maps` (Google Routes / Places New / Weather - needs
`GOOGLE_MAPS_API_KEY`), `transit` (SNCF + IDFM Navitia - needs `SNCF_API_KEY`,
`IDFM_API_KEY`), `google` (user-data class: Gmail search / read / attachment +
**drafts only** - list, read, create (standalone or as a reply, where the server derives
thread, recipient and subject from the parent - `Reply-To` beating `From`) and amend,
each answering with a stable `link`
straight to the draft in the Gmail web UI (`ROSETTA_GMAIL_ACCOUNT` overrides the account
index when the mailbox is not the browser's first) - plus
Calendar list/create/update - deliberately **no send, no delete, no labels**:
the guard is the tool surface itself. Attachments come back transcribed to text
(PDF via pypdf) for reading, or as raw base64 (`raw=True`) for native storage. One-time per-user enrolment at
`/google/enroll` stores the Google refresh token server-side under
`ROSETTA_GOOGLE_DATA`), and `withings` (user-data class, **read-only**: body
measures, daily activity, sleep summaries, workouts and the devices themselves,
enrolled once at `/withings/enroll`). Tool descriptions are intentionally in
**French**: they are runtime UX for the French-speaking agents this hub serves,
not documentation.

The `withings` addon is the hub's only **single-writer** component: Withings
rotates the refresh token on every refresh, invalidating the previous one, so
the stored credential must have exactly one writer. Refreshes are serialized
per user and the access token is cached for its full three hours - but running
two replicas would have them burn each other's token. Keep it at one.
Withings also answers **HTTP 200 for its failures**: the real outcome is the
`status` field inside the JSON body, and a measure arrives as a `(value, unit)`
pair where `unit` is a power of ten (`78192, -3` = 78.192 kg).

## Authentication

Rosetta is an OAuth 2.1 **resource server**. It stores no credentials and no
users: it validates JWT access tokens ([RFC 9068](https://www.rfc-editor.org/rfc/rfc9068))
issued by an external OIDC provider (tested with
[Authelia](https://www.authelia.com/) >= 4.39, clients configured with
`access_token_signed_response_alg` != `none`), against the provider's JWKS.

- Machine agents use the `client_credentials` grant - no human in the loop.
- User-delegated access (future data-holding addons) uses `authorization_code`
  + refresh, or the device code flow for headless bodies.
- [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728) protected-resource
  metadata is served at `/.well-known/oauth-protected-resource` (and per
  addon), and every 401 carries the `WWW-Authenticate` pointer, so OAuth-aware
  MCP clients can discover the authorization server on their own.

Note the trailing slash: the MCP endpoint of an addon is `/<name>/` - `/<name>`
answers with a 307 redirect.

## Configuration

| Env | Default | Purpose |
|---|---|---|
| `ROSETTA_AUTH` | `oidc` | `off` disables auth (local dev only) |
| `ROSETTA_ISSUER` | `https://auth.berard.me` | OIDC issuer (token `iss`) |
| `ROSETTA_AUDIENCE` | external URL | required token `aud` |
| `ROSETTA_EXTERNAL_URL` | `https://rosetta.mcp.berard.me` | public URL (RFC 9728 metadata) |
| `ROSETTA_JWKS_URI` | `<issuer>/jwks.json` | JWKS endpoint override |
| `ROSETTA_ADDONS` | all discovered | comma-separated allowlist |
| `GOOGLE_MAPS_API_KEY` | - | `maps` addon |
| `SNCF_API_KEY`, `IDFM_API_KEY` | - | `transit` addon |
| `ROSETTA_GMAIL_ACCOUNT` | `0` | `google` addon: account segment of the draft web links (`/mail/u/<this>/`) |
| `ROSETTA_GOOGLE_DATA` | `/data/google` | `google` addon: per-user credential store (volume) |
| `WITHINGS_CLIENT_ID`, `WITHINGS_CLIENT_SECRET` | - | `withings` addon: the OAuth app registered on the Withings developer dashboard |
| `ROSETTA_WITHINGS_DATA` | `/data/withings` | `withings` addon: per-user credential store (volume) |
| `TZ` | `Europe/Paris` | local zone used to resolve bare `YYYY-MM-DD` bounds |

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e .[dev]
.venv/bin/pytest
ROSETTA_AUTH=off .venv/bin/uvicorn rosetta.main:app --port 8200
```

## Deployment

Published as `ghcr.io/antorfr/rosetta-mcp` (SemVer tags, `docker-publish`
workflow). Runs as a plain container: port 8200, no volume, configuration by
environment only.
