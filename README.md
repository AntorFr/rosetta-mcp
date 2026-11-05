# rosetta-mcp

**Rosetta** is a modular [MCP](https://modelcontextprotocol.io) hub: one HTTP
service hosting many thin MCP servers (*addons*), each mounted under
its own path, behind a single OIDC bearer-token authentication layer.

Like the stone: one endpoint, and every agent reads it in its own tongue.

```
agent ──Bearer JWT──►  https://rosetta.example.com/<addon>/   (one path per addon, see below)
                       https://rosetta.example.com/health     (unauthenticated, per-addon state)
```

Why a hub: provider API keys stay **server-side** (one deployment, one place to
rotate), agents only ever hold an access token, and adding another MCP is a new
module and nothing else - no new pod, no key sprayed into agent environments.

Where to go next: [`ARCHITECTURE.md`](ARCHITECTURE.md) for the map of the code and its
invariants; `docs/addons.md` for each addon; `docs/donnees.md` for the files the
hub writes; `docs/parcours.md` and `docs/ecrans.md` for the user journeys; `docs/recommandations.md` for the
improvements that would change behaviour, written as options for the operator to decide (none applied).

## Addons

An addon is a module in `src/rosetta/addons/` exposing:

```python
from ._common import new_server

mcp = new_server("name")          # FastMCP: stateless streamable HTTP at "/"
required_env = ["SOME_API_KEY"]   # optional
identity = "user"                 # optional: refuse machine tokens (403)
```

The loader mounts each addon under `/<module-name>` and **isolates failures**.
The MCP endpoint is `/<name>/` (with the trailing slash; `/<name>` answers 307).

| Addon state | Meaning |
|---|---|
| `ok` | mounted, fully operational |
| `degraded` | mounted, but some `required_env` is missing - tools answer with an explicit error |
| `error` | import/startup failed - addon skipped, **hub and other addons stay up** |
| `disabled` | excluded by `ROSETTA_ADDONS` |

Per-addon state is reported on `GET /health`. Modules starting with `_` are shared
helpers, never mounted. Most addons also run standalone over stdio for local debugging
(`python -m rosetta.addons.maps`); `courrier`, `git`, `github`, `postier`, `quotas` and `verbatim` do not (no `__main__` block).

An addon with `identity = "user"` holds per-user credentials server-side, keyed on the
caller's username; machine tokens are refused on its path. Its enrolment routes sit
behind the ingress SSO instead of the hub JWT. Details: `docs/addons.md` and
`docs/donnees.md`.

| Addon | Role | Details |
|---|---|---|
| `courrier` | The family's own Zimbra mailboxes over IMAP: search, read, drafts, disposable OVH aliases (user) | [section](docs/addons.md#courrier) |
| `food` | Open Food Facts, read-only: product by barcode, text search; no key | [section](docs/addons.md#food) |
| `git` | Smart-HTTP git proxy to GitHub, using the `github` addon's token (user) | [section](docs/addons.md#git) |
| `github` | GitHub reads and bounded writes (commit, tag, repo creation, PR merge) as the caller, via a GitHub App (user) | [section](docs/addons.md#github) |
| `google` | Gmail and Google Calendar as the caller: read, drafts, send, events (user) | [section](docs/addons.md#google) |
| `maps` | Google Maps Platform behind one key: routes, places, weather | [section](docs/addons.md#maps) |
| `marees` | Tide times and coefficient for the French coast (api-maree.fr) | [section](docs/addons.md#marees) |
| `meteo` | Hourly wind in knots for sailing spots (Open-Meteo); no key | [section](docs/addons.md#meteo) |
| `postier` | Machine-class mail sending from the assistant's mailbox: locked sender, allowlisted recipients, hourly cap | [section](docs/addons.md#postier) |
| `quotas` | Usage gauges of a Claude subscription and when they reset | [section](docs/addons.md#quotas) |
| `trace` | Walking and cycling routes on OpenStreetMap (BRouter) with elevation, and Overpass points of interest | [section](docs/addons.md#trace) |
| `transit` | Next departures and disruptions, SNCF and RATP/IDFM (Navitia) | [section](docs/addons.md#transit) |
| `verbatim` | What was said in a video: published subtitles split into timestamped sentences (`yt-dlp`) | [section](docs/addons.md#verbatim) |
| `withings` | The caller's Withings data, read-only: activity, body measures, sleep, workouts, devices (user) | [section](docs/addons.md#withings) |

## Authentication

Rosetta is an OAuth 2.1 **resource server**. It stores no credentials and no
users: it validates JWT access tokens ([RFC 9068](https://www.rfc-editor.org/rfc/rfc9068))
issued by an external OIDC provider (tested with
[Authelia](https://www.authelia.com/) >= 4.39, clients configured with
`access_token_signed_response_alg` != `none`), against the provider's JWKS.

- Machine agents use the `client_credentials` grant - no human in the loop.
- User-delegated access (the `identity = "user"` addons) uses `authorization_code`
  + refresh, or the device code flow for headless bodies.
- [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728) protected-resource
  metadata is served at `/.well-known/oauth-protected-resource` (and per
  addon), and every 401 carries the `WWW-Authenticate` pointer, so OAuth-aware
  MCP clients can discover the authorization server on their own.

The token normally travels as `Authorization: Bearer`. A **Basic** header is
accepted as an alternative envelope, taking the *password* as the token - which
exists for exactly one caller: **git cannot be taught to send a Bearer header**,
since a credential helper hands it a username and a password and nothing else
(GitHub's own `x-access-token:<token>` convention). This widens the envelope, not
the trust: the token inside faces the same signature, issuer, audience and expiry
checks.

The **challenge** had to follow, and only under `/git`: git does not understand a
`Bearer` challenge, and faced with one it gives up on "Authentication failed"
**without ever asking its credential helper** — so a pod holding a valid token
could not push, and the helper meant to carry the channel and the shield could
never have run. A 401 under `/git` therefore answers `Basic realm="rosetta"`;
everywhere else the RFC 9728 pointer is unchanged, and no browser is ever invited
to prompt on an MCP endpoint.

Note the trailing slash: the MCP endpoint of an addon is `/<name>/` - `/<name>`
answers with a 307 redirect.

One deliberate exception to "everything needs a token": the MCP
**surface-reading** methods - `initialize`, `notifications/initialized`,
`ping`, `tools/list` - are served **without a token** on the mount roots, so
an external catalog observer (a policy gateway watching the tool surface for
drift) can poll them while deliberately holding no hub credential. The window
is narrow and fail-closed on every edge: POST only (the SSE stream stays
authenticated), single-segment paths only (never `/git/`'s bare-HTTP routes),
one well-formed JSON-RPC object only (a batch is refused even when every
element is listed, as are unreadable or oversized bodies), and only when no
token was presented at all - an *invalid* token is refused even on a listed
method. Everything else, `tools/call` first, keeps the exact 401 + discovery
pointer above. Tool names and schemas thereby become readable without a
token: an accepted trade-off for a hub on an internal network, behind a
gateway meant to be its only caller.


## Configuration

Everything is read from the environment (with `ROSETTA_AUTH=off`, `ROSETTA_ISSUER` and `ROSETTA_EXTERNAL_URL` are not needed to start). "Required" means the hub or the addon does not
work without it. The hub does not refuse to start on a missing one; an addon's
`required_env` mounts that addon `degraded` and nothing else.

| Variable | Scope | Required | Default | Meaning |
|---|---|---|---|---|
| `ROSETTA_AUTH` | hub | no | `oidc` | `off` disables auth (local dev only) |
| `ROSETTA_ISSUER` | hub | yes (unset, no default: tokens cannot be validated without it) | none | OIDC issuer (token `iss`) |
| `ROSETTA_EXTERNAL_URL` | hub, `git`, `github`, `google`, `quotas`, `trace`, `withings` | yes (unset, no default: metadata, callbacks and links are unusable without it) | none | public URL of the hub (RFC 9728 metadata, OAuth callbacks, enrolment links) |
| `ROSETTA_AUDIENCE` | hub | no | `ROSETTA_EXTERNAL_URL` | accepted token `aud`; a token audienced to `<audience>/<addon>` is accepted only on that addon |
| `ROSETTA_JWKS_URI` | hub | no | `<issuer>/jwks.json` | JWKS endpoint override |
| `ROSETTA_TRUSTED_ISSUERS` | hub | no | empty | extra issuers accepted for validation only, comma-separated, each `<issuer>` or `<issuer>=<jwks_uri>` |
| `ROSETTA_ADDONS` | hub | no | all discovered | comma-separated allowlist of addons to mount |
| `TZ` | `google`, `marees`, `meteo`, `quotas`, `withings` | no | `Europe/Paris` | local zone for bare dates and "today" |
| `GOOGLE_MAPS_API_KEY` | `maps` | yes | none | Google Maps Platform key |
| `SNCF_API_KEY` | `transit` | yes | none | SNCF Navitia key |
| `IDFM_API_KEY` | `transit` | yes | none | IDFM PRIM key |
| `API_MAREE_KEY` | `marees` | yes | none | api-maree.fr key |
| `ROSETTA_WIND_SPOTS` | `meteo` | no | empty | named spots as JSON, `{"La Torche": "47.8367,-4.3492"}`; read per call |
| `ROSETTA_GOOGLE_DATA` | `google` | no | `/data/google` | per-user credential store (volume) |
| `ROSETTA_GMAIL_ACCOUNT` | `google` | no | `0` | account index in the draft web links (`/mail/u/<this>/`) |
| `GITHUB_CLIENT_ID` | `github` | yes | none | GitHub App client id |
| `GITHUB_CLIENT_SECRET` | `github` | yes | none | GitHub App client secret |
| `GITHUB_OWNER` | `github` | no | `AntorFr` | account under which `repo_create` creates repositories |
| `ROSETTA_GITHUB_DATA` | `github` | no | `/data/github` | per-user token store (volume) |
| `ROSETTA_GIT_REPOS` | `git` | no | empty | `owner/name` allowlist, comma-separated; empty = every repository the App reaches |
| `WITHINGS_CLIENT_ID` | `withings` | yes | none | Withings OAuth app client id |
| `WITHINGS_CLIENT_SECRET` | `withings` | yes | none | Withings OAuth app secret |
| `ROSETTA_WITHINGS_DATA` | `withings` | no | `/data/withings` | per-user credential store (volume) |
| `ROSETTA_QUOTAS_DATA` | `quotas` | no | `/data/quotas` | per-user credential store (volume) |
| `ROSETTA_QUOTAS_OWNER` | `quotas` | only with several enrolled subjects | inferred when exactly one is enrolled | whose gauges a machine token reads |
| `ROSETTA_QUOTAS_CLIENTS` | `quotas` | no | empty = every machine identity the hub authenticated | comma-separated machine subjects allowed to read |
| `ROSETTA_CLAUDE_UA` | `quotas` | no | `claude-code/2.1.274` | User-Agent sent to Anthropic's usage endpoint; an unrecognised one gets rate-limited |
| `OFF_USER_AGENT` | `food` | no | `Alfred/1.0 (contact@antor.fr)` | User-Agent for Open Food Facts |
| `BROUTER_URL` | `trace` | no | `https://brouter.de/brouter` | routing engine; read per call |
| `OVERPASS_URL` | `trace` | no | `https://overpass-api.de/api/interpreter` | OSM point lookups |
| `OVERPASS_USER_AGENT` | `trace` | no | `rosetta-mcp/trace (contact@antor.fr)` | User-Agent sent to Overpass |
| `IGN_ALTI_URL` | `trace` | no | IGN Géoplateforme elevation endpoint | used only for `altimetrie="ign"` |
| `MAIL_IMAP_HOST` | `courrier` | yes | none | IMAP host of the mailboxes (port 993, TLS) |
| `MAIL_DOMAIN` | `courrier` | yes | none | family mail domain |
| `MAIL_DRAFTS_FOLDER` | `courrier` | no | `Drafts` | IMAP folder receiving drafts |
| `MAIL_VAULT_ADDR` | `courrier` | yes in practice (mailbox passwords come only from the vault) | empty | Vault (OpenBao) address from which mailbox passwords are read |
| `MAIL_VAULT_MOUNT` | `courrier` | no | `jwt-authelia` | Vault JWT auth mount |
| `MAIL_VAULT_ROLE` | `courrier` | no | `rosetta-mail` | Vault role |
| `OVH_APPLICATION_KEY` | `courrier` | no | none | OVH API key, needed for the alias tools |
| `OVH_APPLICATION_SECRET` | `courrier` | no | none | OVH API secret, same |
| `OVH_CONSUMER_KEY` | `courrier` | no | none | OVH consumer key, same |
| `POSTIER_FROM` | `postier` | yes | none | sender address and login, fixed |
| `POSTIER_PASSWORD` | `postier` | yes | none | mailbox password |
| `POSTIER_ALLOWED` | `postier` | no, but without it every recipient is refused | empty | comma-separated recipient patterns (`fnmatch`) |
| `POSTIER_MAX_PER_HOUR` | `postier` | no | `10` | sliding-window hourly cap |
| `POSTIER_SMTP_HOST` | `postier` | no | `smtp.mail.ovh.net` | SMTP host |
| `POSTIER_IMAP_HOST` | `postier` | no | `imap.mail.ovh.net` | IMAP host for the copy in the sent folder |
| `POSTIER_SENT_FOLDER` | `postier` | no | `Sent` | sent folder |

## Development

CI (the `test` job of `.github/workflows/docker-publish.yml`, which the `build` job needs)
runs, on Python 3.12:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .[dev]
.venv/bin/ruff check .                                # lint
.venv/bin/pytest --cov --cov-report=term-missing      # tests + coverage
ROSETTA_AUTH=off .venv/bin/uvicorn rosetta.main:app --port 8200   # local start (not CI)
```

Once started with auth off, an addon answers MCP over POST on its slash-terminated path, e.g.
`curl -s -X POST localhost:8200/food/ -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'`.

To run it as the container (auth off, no secrets, local only):

```bash
docker build -t rosetta-mcp .
docker run --rm -p 8200:8200 -e ROSETTA_AUTH=off rosetta-mcp   # then: curl localhost:8200/health
```

Python ≥ 3.12 is required (`pyproject.toml`). Coverage fails below `fail_under` in
`pyproject.toml`; lint ignores are listed there with their reason. `ROSETTA_AUTH=off` disables
token validation, for local development only. With no secrets set, `/health`
answers `degraded` and names the addons whose variables are missing: the expected state of a
bare local start. `tests/test_surface.py` pins the tools, routes, health and environment
variables read by the code against `tests/surface.json`; after an intended change, rewrite the snapshot with
`python tests/test_surface.py --write` and review the diff.

## Deployment

Published as `ghcr.io/antorfr/rosetta-mcp` (SemVer tags, `docker-publish` workflow). Plain
container, port 8200, runs as user `rosetta` (uid 1000), configured by environment.

The addons that store credentials (`google`, `github`, `withings`, `quotas`) write per-user files under
`/data` (`/data/google`, `/data/github`, `/data/withings`, `/data/quotas` by default, each
overridable, see Configuration). The image creates no `/data` and declares no `VOLUME`:
mount a persistent volume at `/data`, writable by uid 1000, or credentials are lost with the
container. Formats: `docs/donnees.md`.
