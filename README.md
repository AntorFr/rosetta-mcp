# rosetta-mcp

**Rosetta** is a modular [MCP](https://modelcontextprotocol.io) hub: one HTTP
service hosting many thin, read-only MCP servers (*addons*), each mounted under
its own path, behind a single OIDC bearer-token authentication layer.

Like the stone: one endpoint, and every agent reads it in its own tongue.

```
agent ──Bearer JWT──►  https://rosetta.example.com/maps/      (Google Maps, Places, Weather)
                       https://rosetta.example.com/meteo/     (Open-Meteo, wind for sailing)
                       https://rosetta.example.com/transit/   (SNCF + IDFM/Navitia)
                       https://rosetta.example.com/trace/     (BRouter + Overpass, walks on OSM)
                       https://rosetta.example.com/marees/    (tide times + coefficient, France)
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
`GOOGLE_MAPS_API_KEY`; the three weather tools - current, daily and
hour-by-hour - all report **wind with its gust** and a bearing in degrees plus
a French 16-point cardinal derived from them, never Google's `cardinal` enum.
`weather_hourly` stops at 24 h because the upstream `pageSize` caps there, and
one page is enough to answer when the rain starts), `transit` (SNCF + IDFM Navitia - needs `SNCF_API_KEY`,
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
`ROSETTA_GOOGLE_DATA`), `withings` (user-data class, **read-only**: body
measures, daily activity, sleep summaries, workouts and the devices themselves,
enrolled once at `/withings/enroll`), and `github` (user-data class, for the
coding agent: nine read tools — repo listing, file, tree, commits, code search,
tags, Actions runs, plus pull requests (list, and one in detail with its merge
state, changed files and, on request, the patch) — and exactly **three** write
tools, `repo_commit` (create / modify / **delete** in one atomic commit through
the Git Data API; a `null` content deletes, so deletion is never a separate
capability to unlock), `repo_tag`, and `pull_request_merge`. Deliberately
**absent**, and that absence *is* the guarantee rather than a hook: repository
creation or deletion, forks, branch deletion, force-push, issues, opening /
closing / commenting / reviewing a pull request, Actions secrets, settings,
collaborators.

Two upstream behaviours shape the pull request tools. GitHub computes
mergeability **asynchronously**: the first read of a dormant PR answers
`mergeable: null` and starts a background job, so the addon re-reads rather than
handing an agent a null it would read as "not mergeable" — a silent false
negative. And a merge carries the head SHA that was just read, so a branch that
moved in between earns a 409 instead of merging something nobody looked at.
The branch is never deleted afterwards: that tool does not exist here.

Needs a **GitHub App** with *Expire user authorization tokens* enabled — without
it GitHub issues no refresh token — declaring `contents: write`,
`metadata: read`, `actions: read`, **`pull requests: read`** (merging itself
goes through `contents: write`) and, easily forgotten, **`workflows: write`**
for any commit touching `.github/workflows/`. Credentials via
`GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET`, per-user tokens under
`ROSETTA_GITHUB_DATA`, enrolled once at `/github/enroll`), and `food`
(Open Food Facts: barcode(s) — a whole shopping basket in one call — to name,
brand, ingredients, allergens, additives, nutriments per 100 g, Nutri-Score,
NOVA and Eco-Score, plus a free-text search as a fallback. **No key, no
account, no enrolment**: read access is anonymous, so this addon carries no
secret and stays machine class. Open Food Facts is community-*editable* and no
writing tool exists here — that absence is what stops an agent publishing into
a public database on the user's behalf. Calls are rate-limited **in-process**
because the upstream quota, 15 product reads and 10 searches per minute, is
counted per **IP** — i.e. per deployment, shared with every other service
behind the same egress — and exceeding it earns a ban for all of them),
`git` (a smart-HTTP proxy rather than a tool surface — see its own section below).
Finally `meteo` (Open-Meteo: hour-by-hour **wind in knots** - mean, gust and
gust ratio, bearing - for planning a sail. Like `food` it needs no key, no
account and no enrolment, so it holds no secret and stays machine class.
Two tools: `wind_forecast`, bounded to the spot's daylight hours and able to
run the same slot past several models, and `wind_spots` over a registry given
in `ROSETTA_WIND_SPOTS`.

Three upstream behaviours drive the design, all measured against the live API
rather than read off the docs:

- A model asked **outside its domain vanishes from a batched response** - no
  error, no null column, HTTP 200 - while the same model asked **on its own**
  answers an honest `400 / "No data is available for this location"`.
- The response key is suffixed by **how many models survived**, not by how many
  were asked: two requested and one returned yields the bare `wind_speed_10m`,
  so a naive parser files one model's numbers under another's name.
- Past its **horizon** a model does the opposite and returns `null` rows.

Hence: **one HTTP request per model, never a batched one**, which makes the
first two impossible rather than merely handled, and nulls are dropped instead
of being read as a flat calm. Wind is requested in knots natively
(`wind_speed_unit=kn`) and in the spot's own zone (`timezone=auto` - pinning
the house zone onto a spot elsewhere pushes sunset onto the next calendar day
and collapses the daylight window). Bearings are averaged as a **circular**
mean, because 350° and 10° average to north, not to south. The data is
**CC-BY 4.0**, so every answer names its source).

Last, `trace` (walking and hiking routes over OpenStreetMap, **no key, no
account**: `trace_calcule` routes an ordered list of points on foot or by bike
through [BRouter](https://brouter.de), and `trace_pois` finds what a walker
needs — drinking water, viewpoints, shelters, benches, waymarked trails —
through Overpass. Sightseeing landmarks stay with `search_places` in `maps`:
ratings, review *counts* and opening hours are Google's and OSM has no
equivalent, so the two sources meet in the caller's own file, field by field,
each keeping its provenance).

Its design rule is that **the geometry never travels through the model**:
`trace_calcule` answers with the numbers — distance, D+/D−, surfaces, metres of
steps, per-leg distances, and how far each requested point fell from the track
— plus a URL. `GET /trace/geometrie` then returns the track
[polyline-encoded](https://developers.google.com/maps/documentation/utilities/polylinealgorithm)
for the caller to write straight to disk. A 3 km town loop is 328 points and
13 kB of GPX; retyped by a language model, one dropped character shifts the
whole tail of the walk. The route is **stateless** — the URL carries the same
parameters, so it recomputes rather than reading a cache that would need a
lifetime, a size and a replica count.

Four upstream behaviours drive it, all measured against the live services on
2026-08-04 rather than read off the docs:

- BRouter takes **`lon,lat`** pairs, the reverse of every other tool here. The
  flip lives in exactly one function, because getting it wrong yields a
  plausible route in the wrong hemisphere rather than an error.
- Its failures are **not JSON**: an unroutable pair is HTTP 400 with a
  plain-text body (`target island detected for section 0`), an unknown profile
  is HTTP 500 with an **empty** one. Parsing before checking the status turns a
  usable message into a decoding traceback. Profile names are case-sensitive,
  the only foot profile is `hiking-beta`, and `trekking` is a **bicycle**
  profile despite the name.
- Ascent is computed here with a **5 m hysteresis filter**, one method for both
  directions so a loop reports the same figure twice. Raw accumulation inflated
  a real 9.5 km Chartreuse loop from 378 m to 506 m.
- Overpass emits in its own order and truncates on a **pooled** limit, so
  asking for `eau,vue,banc` around a town came back as benches only, with both
  wanted types crowded out. Each type gets its own named set and its own limit,
  and the answer is grouped by type.

A requested point anchors to its nearest track vertex **searching forward
only**, from the previous anchor. On a loop the last point *is* the first one,
and a global nearest-vertex search snaps it back to index 0 — turning a final
260 m stroll home into a 2 770 m leg measured the wrong way round the town.

`altimetrie="ign"` re-profiles a track on IGN **RGE ALTI** (1 m grid over
France, free, no key) instead of BRouter's own model. On that Chartreuse loop
the two agreed to 1 %, so it is an option rather than the default — it earns
its round trip only on fine terrain. Two IGN quirks: the service **resamples
evenly along the line** instead of answering at the vertices it was given, and
booleans must be sent as the **strings** `"true"` / `"false"` (a real JSON
boolean is rejected with `BAD_PARAMETER`).

And `marees` (French tide **times** and **coefficient** — nothing else: no
height curve, no range, no threshold windows, no nautical routing. The two
questions it answers are "is the foreshore walkable at three?" and "will the
golfe run hard on Saturday?", and both need a handful of times and one number).

⚠️ **The coefficient is not local.** It is computed for the port of **Brest**
and holds identically along the Channel and Atlantic coasts, the tidal wave
reaching them barely distorted. What varies with place is the TIMES. The same
100 means ~6 m of range at Brest, over 13 m at Mont-Saint-Michel, and 0.5 m in
the Mediterranean — where the notion is meaningless. Hence the shape of every
answer: one coefficient per tide, times per port.

Source: [api-maree.fr](https://api-maree.fr), computing water levels from the
Ifremer/PREVIMER harmonic constituents — free, one account key, 360 requests
per hour, window bounded to **J−30 → J+30**. Its coefficient is stated by the
source itself as **non-official**: the authority is the Shom, which *sells* its
SPM/SAPM service. Good enough to decide a walk or a session, not good enough
for anything where the official figure is binding — and every answer says so.

Two guards live in the code rather than only in this file: the **distance** to
the reference port is always returned and flagged past 25 km (a port 50 km away
does not predict your foreshore), and a date outside J±30 is **refused** rather
than extrapolated. A place is resolved by name against the 131 known ports
first, then through the keyless **IGN geocoder** — no third key for a question
that is already answered by open data.

Tool descriptions are intentionally in **French**: they are runtime UX for the
French-speaking agents this hub serves, not documentation.

`withings` and `github` are the hub's **single-writer** components: both
rotate the refresh token on every refresh, invalidating the previous one, so
the stored credential must have exactly one writer. Refreshes are serialized
per user and the access token is cached for its full three hours - but running
two replicas would have them burn each other's token. Keep it at one.
Withings also answers **HTTP 200 for its failures**: the real outcome is the
`status` field inside the JSON body, and a measure arrives as a `(value, unit)`
pair where `unit` is a power of ten (`78192, -3` = 78.192 kg).

### The `git` addon — a smart-HTTP proxy, not a tool surface

`repo_commit` publishes file **contents**, passed inline in the tool call: an
agent must retype every byte it wants to publish. Above a few kilobytes that is
a lossy channel, and a corrupted retype rewrites a source file silently. The
`git` addon removes the retyping entirely — the pod pushes real git, so the
object that was verified is the object that gets published.

It is therefore the one addon whose surface is **plain HTTP rather than MCP**:
git's smart-HTTP endpoints, mounted per repository.

```
GET  /git/<owner>/<repo>/info/refs?service=git-receive-pack|git-upload-pack
POST /git/<owner>/<repo>/git-receive-pack     # push
POST /git/<owner>/<repo>/git-upload-pack      # fetch / clone
```

The caller authenticates with its ordinary hub token; the addon resolves that
identity to the same GitHub App credential the `github` addon already refreshes,
and streams the exchange to github.com. **The GitHub credential never leaves the
hub** — which is the whole point: a pod can push without ever holding one, so a
compromised agent cannot walk off with it.

What it refuses, before a byte reaches GitHub:

| Refused | Why |
|---|---|
| deleting any ref | destructive, and never needed from an agent |
| a ref outside `refs/heads/*` and `refs/tags/*` | nothing else has business being pushed |
| moving an existing tag | a tag is what a published image was built from |
| a non-fast-forward push | see below — GitHub decides, `force: false` |
| a push updating several refs at once | one promotion per request |
| `Content-Encoding` on a receive-pack body | it would blind the inspection |

The non-fast-forward rule must not be mistaken for belt-and-braces. **The wire
protocol carries no force flag**: the server decides by ancestry, and GitHub
accepts a force-push on an unprotected branch. Relaying verbatim would therefore
protect nothing.

⚠️ **0.14.x asked the wrong oracle, and refused everything.** It called
`/compare/<old>...<new>` *before* relaying the pack — but `<new>` is precisely
the commit being pushed, so GitHub answered `404`, which the addon read as
"ancestry unverifiable" and refused. Every update of an existing branch was
rejected; only branch creations got through. The 16 tests missed it because their
mock answered `ahead` for any sha, existing or not.

**0.15.0 asks the only party that can answer, and asks it last.** A branch update
is streamed to a throwaway ref (`refs/heads/rosetta-scratch/<hex>`, created from
zero — nothing there can be overwritten), which lands the objects on GitHub.
The real ref is then moved with `PATCH /git/refs/…` and **`force: false`**, which
GitHub itself refuses unless the update is a fast-forward — the guarantee now
comes from the server that owns the branch, not from a question asked too early.
The scratch ref is dropped in a `finally`. Only the command prefix is rewritten,
so the pack still streams without ever being buffered.

Two consequences worth knowing. A push touching **several refs at once** is
refused: one promotion per request. And a refusal now comes back as a git
**`ng` line** in the report-status rather than an HTTP 403 — once the pack is
flowing git wraps the exchange in a side-band and an error body never reaches
the user, who sees only `RPC failed; HTTP 403` with no reason at all.

Deciding *whether to push at all* is deliberately **not** here. Only the calling
side can know whether a human is in front, so that judgement belongs to the git
credential helper in the pod; this addon enforces what is visible on the wire.

Only the ref rules are unconditional: `ROSETTA_GIT_REPOS` can additionally narrow
the proxy to an explicit set of repositories, and is empty (no restriction) by
default — the App's own installation scope is the outer bound.

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

## Configuration

| Env | Default | Purpose |
|---|---|---|
| `ROSETTA_AUTH` | `oidc` | `off` disables auth (local dev only) |
| `ROSETTA_ISSUER` | `https://auth.berard.me` | OIDC issuer (token `iss`) |
| `ROSETTA_AUDIENCE` | external URL | required token `aud` |
| `ROSETTA_EXTERNAL_URL` | `https://rosetta.mcp.berard.me` | public URL (RFC 9728 metadata) |
| `ROSETTA_JWKS_URI` | `<issuer>/jwks.json` | JWKS endpoint override |
| `ROSETTA_ADDONS` | all discovered | comma-separated allowlist |
| `ROSETTA_GIT_REPOS` | *(empty)* | `git` addon: optional `owner/name` allowlist for the smart-HTTP proxy. Empty = every repository the App can reach; the ref rules apply either way |
| `GOOGLE_MAPS_API_KEY` | - | `maps` addon |
| `SNCF_API_KEY`, `IDFM_API_KEY` | - | `transit` addon |
| `ROSETTA_GMAIL_ACCOUNT` | `0` | `google` addon: account segment of the draft web links (`/mail/u/<this>/`) |
| `ROSETTA_GOOGLE_DATA` | `/data/google` | `google` addon: per-user credential store (volume) |
| `WITHINGS_CLIENT_ID`, `WITHINGS_CLIENT_SECRET` | - | `withings` addon: the OAuth app registered on the Withings developer dashboard |
| `ROSETTA_WITHINGS_DATA` | `/data/withings` | `withings` addon: per-user credential store (volume) |
| `OFF_USER_AGENT` | `Alfred/1.0 (contact@antor.fr)` | `food` addon: Open Food Facts requires a custom User-Agent naming the app, or treats the caller as a bot |
| `BROUTER_URL` | `https://brouter.de/brouter` | `trace` addon: routing engine. The public instance is a courtesy service with no SLA; self-hosting (`abrensch/brouter` + the `segments4` tiles for the area) is a URL change, never a rewrite — which is why it is read per call |
| `OVERPASS_URL` | `https://overpass-api.de/api/interpreter` | `trace` addon: OSM point lookups. The quota is counted per **IP**, i.e. per deployment — one grouped request per call, never a loop |
| `OVERPASS_USER_AGENT` | `rosetta-mcp/trace (contact@antor.fr)` | `trace` addon: Overpass expects a descriptive agent naming a contact |
| `IGN_ALTI_URL` | `https://data.geopf.fr/altimetrie/…/elevationLine.json` | `trace` addon: IGN Géoplateforme elevation, used only when a call asks for `altimetrie="ign"`. Free, keyless, France only |
| `API_MAREE_KEY` | - | `marees` addon: free account key from api-maree.fr (360 req/h). Absent = the addon mounts **degraded** and says so, the hub stays up |
| `ROSETTA_WIND_SPOTS` | *(empty)* | `meteo` addon: named sailing spots as JSON, `{"La Torche": "47.8367,-4.3492"}` or `{"La Torche": {"latlng": "…", "note": "…"}}`. Read per call, so adding a spot is a rollout, not a rebuild; a malformed entry is logged and skipped rather than taking the registry down |
| `TZ` | `Europe/Paris` | local zone used to resolve bare `YYYY-MM-DD` bounds (and, in `meteo`, only to decide what "today" means - forecasts use the spot's own zone) |

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
