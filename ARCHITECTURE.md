# Rosetta — architecture

Status of this file: the *Organs*, *Security* and *Deployment* sections describe the code
as it is after the `rosetta-qualite` cleanup (v0.30.0); `AD-n` entries are decisions, the
first ones taken by the quality epic; the `AD-doc-n` of *Durable decisions from the
logbook* are the choices that the retired `.agent/status.md` taught and the code still
shows.

## Invariants

- **I-1** Provider API keys and downstream credentials live server-side only; agents
  hold an identity token and nothing else.
- **I-2** One addon failing never takes the hub or the other addons down.
- **I-3** Adding an addon is adding a module: no change to the hub.
- **I-4** The quality epic changes no behaviour (B-9 in `docs/parcours.md`).

## Organs

Every module of `src/rosetta/` outside the addons, then the addon contract and its helpers.

- `src/rosetta/__init__.py` — the package docstring and `__version__`, read from the
  installed distribution metadata (`importlib.metadata`, distribution `rosetta-mcp`) so
  that `pyproject.toml` is the single source; `0+unknown` when the package is not
  installed. `/health` and `/` report this value.
- `src/rosetta/main.py` — addon discovery (`pkgutil`, `ROSETTA_ADDONS` allowlist,
  `_`-prefixed modules are helpers; each module is imported or reloaded, so a second
  `create_app()` gets fresh FastMCP instances), state `ok`/`degraded`/`error`/`disabled`
  (`degraded` = a name of the addon's `required_env` is unset), ASGI app assembly
  (Starlette `Mount` per addon at `/<name>`, the addon's `extra_routes` registered before
  the mount, `/health`, `/`, the two RFC 9728 documents), per-addon session manager
  lifespan (a start failure downgrades that addon to `error`). It tells the auth
  config which prefixes are user-only or open, then wraps the app in
  `BearerJWTMiddleware`. The app is built at import (`app = create_app()`).
- `src/rosetta/auth.py` — `AuthConfig` (read from `ROSETTA_AUTH`, `ROSETTA_ISSUER`,
  `ROSETTA_EXTERNAL_URL`, `ROSETTA_AUDIENCE`, `ROSETTA_JWKS_URI`, `ROSETTA_TRUSTED_ISSUERS`),
  `BearerJWTMiddleware` (OIDC JWT validation: signature against the JWKS of the issuer the
  token claims if it is trusted, `iss`, `aud` global or `<audience>/<addon>`, `exp`/`iat`
  required; `identity = "user"` enforcement; open paths; the anonymous catalogue window),
  `token_from_header` (Bearer, or the password of a Basic header), the `current_claims` and
  `current_token` context variables addons read, and `protected_resource_metadata`
  (RFC 9728).
- `src/rosetta/addons/__init__.py` — the package marker and docstring only: one module =
  one MCP server mounted under `/<module-name>`; `_`-prefixed modules are never mounted.
- `src/rosetta/addons/<name>.py` — one FastMCP server per addon; contract (documented in the
  `main.py` docstring): `mcp`, optional `required_env`, `identity`, `extra_routes`,
  `open_paths`. The per-addon reference is `docs/addons.md`.
- `src/rosetta/addons/_common.py` — helpers shared by addons, never mounted:
  `new_server` (FastMCP for hub mounting: stateless streamable HTTP at the mount root, SDK
  DNS-rebinding protection off since the hub's JWT layer protects), `http_client(transport, …)`,
  `current_subject`/`remote_user` (the store key from the token claims / from the ingress
  `Remote-User` header, both NFC-normalised so they match), the identity bridge
  (`bridge_learn`/`bridge_resolve`), `enrol_page` (the card shown by enrolment routes),
  `dig` (tolerant nested lookup), per-subject store `read_user`/`write_user` (atomic,
  mode 600), signed OAuth `state` (`state_key`/`sign_state`/`verify_state`/`state_codec`).
- Persistence: per-subject files, see `docs/donnees.md`. The hub itself keeps no state.

### Security

- **Authentication**: the hub is an OAuth 2.1 resource server. Everything but `/health` and
  `/.well-known/*` (and the `open_paths` of addons, guarded by the ingress forwardAuth
  instead) needs a valid JWT: algorithms `RS256`, `PS256`, `ES256`, `EdDSA`; issuer in
  `ROSETTA_ISSUER` or `ROSETTA_TRUSTED_ISSUERS`; audience `ROSETTA_AUDIENCE` (default the
  external URL), or `<audience>/<addon>` on that addon's mount only. Keys are fetched lazily
  per issuer, so the hub boots when an IdP is down. `ROSETTA_ISSUER` and
  `ROSETTA_EXTERNAL_URL` have no default (the repository is public). `ROSETTA_AUTH=off`
  disables the layer, for local development only.
- **Identity classes**: a subject starting `oauth2:client:` is a machine; an addon with
  `identity = "user"` answers 403 to it. Machine tokens are the default class.
- **Failures**: 401 carries `WWW-Authenticate` pointing at the RFC 9728 metadata, except
  under `/git/` where it is `Basic realm="rosetta"` (see AD-doc-4).
- **Secrets**: provider keys come from the environment (`required_env`), user credentials
  from per-subject files created mode 600, written atomically; none is returned in a tool
  answer (I-1).
- **Network**: no TLS here, the ingress terminates it; the container listens on 8200.

### Deployment

- Multi-stage `Dockerfile`: a `python:3.12-slim` build stage installs the package into a
  virtualenv (`/opt/venv`) from `pyproject.toml` and `src`; the runtime stage copies only the
  venv, runs as non-root user `rosetta` (uid 1000), exposes 8200, and starts
  `uvicorn rosetta.main:app --host 0.0.0.0 --port 8200`.
- `HEALTHCHECK` GETs `/health` on 127.0.0.1:8200 (unauthenticated by design, also the
  Kubernetes probe) and requires status 200; a degraded addon still gives 200.
- Image `ghcr.io/antorfr/rosetta-mcp` published by `.github/workflows/docker-publish.yml`
  (the attestation of the suite gates the build, AD-preuve-1; before that AD-outillage-2); manifests live in `AntorFr/k8s-home-lab`
  (not touched here, AD-1).
- Single replica: the rotating refresh tokens of `github` and `withings`, the sliding
  windows of `food` and `postier` are in-process state (AD-doc-18).

## Decisions

### AD-1 — Scope stays in `rosetta-mcp`
The epic's only target repository is `AntorFr/rosetta-mcp`. `k8s-home-lab` is not a
target: deployment is reserved to the operator and nothing here changes the image
contract. Rejected: opening a `k8s-home-lab` tranche (it would change production for a
no-functional-change epic). Reversed if a documentation fix proves to need a manifest change.

### AD-2 — Documentation layout
`README.md` (what/how to run/configure, operator-facing), `ARCHITECTURE.md` (organs,
`AD-n`, rejections), `docs/addons.md` (per-addon reference), `docs/donnees.md`,
`docs/parcours.md`, `docs/ecrans.md` (stating there is none); `docs/recommandations.md` (findings left
for the operator to decide, AD-7).
Language: English for `README.md` and `ARCHITECTURE.md`; `docs/addons.md`
is in French, as the tool descriptions it quotes; the other `docs/` pages are in English. Rejected: one giant README (it is already
555 lines and wrong in places); a wiki (a pod reads only the repo).

### AD-3 — `.agent/status.md` is retired
Its lasting decisions are distilled into `ARCHITECTURE.md`; the file is removed and
stays in git history. Rejected: keeping it as `docs/history.md` — 1000 lines of
logbook, with obsolete checkboxes and pod-level infrastructure detail, mislead a pod
more than they inform it.

### AD-4 — "No functional change" is made testable
Before any refactor, a characterisation test pins the tool surface (every addon's tool
names and input schemas, the route list, `/health` shape) and the env variable names
read by the code. Refactors must keep it and the existing tests green. Rejected:
trusting review alone.

### AD-5 — Shared helpers go to `_common.py`, behaviour-preserving
The helpers copied across addons (HTTP client factory, per-user file store, signed
OAuth `state`, current-subject lookup, timezone, paging) are extracted one family per
task, each with the addon tests as the proof. Not touched: public tool signatures,
docstrings that agents read as tool descriptions (they are part of the contract).
Rejected: splitting `google.py` & co. into packages now — a larger diff for no
behavioural gain; listed as a recommendation.

### AD-6 — Tooling: ruff and coverage (CI part replaced by AD-preuve-1), mypy recommended only
`pyproject.toml` gets a `ruff` configuration (lint, with the findings of the audit fixed
or explicitly ignored with a reason) and `pytest-cov` in the `dev` extra; CI runs lint
and tests with coverage on pull requests and gates the image build on them. The
coverage floor is set from the measured value (no invented target). mypy is not imposed:
the code base is annotated unevenly, and introducing it is a typing project, kept as a
recommendation. Reversed if a measured first run shows few errors.

### AD-7 — Findings that change behaviour become recommendations
Hard-coded personal contacts in default User-Agents (`contact@antor.fr`), the undocumented
Anthropic endpoint of `quotas`, unpinned `yt-dlp`, base image not pinned by digest,
Actions pinned by tag: each is to be written in `docs/recommandations.md` (R-1 to R-11, all `open`; state and
how to extend it: AD-recommandations-1) with options and a recommendation. None is applied in this epic, because changing a default or a pin is a
visible change the operator owns.

### AD-filet-1 — The characterisation test reads the code, one snapshot file pins it
Detailed by `rosetta-qualite-filet`. `tests/test_surface.py` builds the surface from the
shipped code and compares it with `tests/surface.json`, a versioned snapshot written once
from v0.29.0 and never regenerated by a refactor (a diff in it is a behaviour change and
is reviewed as one; when a tool, route or variable changes on purpose, rewrite it with
`python tests/test_surface.py --write` and review the diff). Four sections, each sorted so the file is stable:
- **tools** — per addon (every module of `rosetta.addons` not starting with `_`), read
  through the FastMCP instance (`await mcp.list_tools()`): name, `description` (the
  docstring agents read, AD-5) and `inputSchema`. The description goes through
  `inspect.cleandoc`: Python 3.13 dedents docstrings and 3.12 does not, and the
  snapshot must not depend on the interpreter.
- **routes** — from `create_app()` (auth off): every top-level route path with its
  methods, every addon `Mount` path, plus each addon's `extra_routes` and `open_paths`
  and its `identity`; the extra routes appear at `/<addon><suffix>`.
- **health** — the key set and value types of `/health` (`status`, `version`, `addons`)
  and, per addon, its state in a test environment with no secrets (the version value
  itself is excluded: a release bump is not a surface change).
- **env** — the names of environment variables read by `src/rosetta/**.py`, found by
  parsing the sources (`ast`): the literal first argument of `os.environ.get(...)`,
  `os.getenv(...)` and `os.environ[...]`, every literal of a module-level `required_env`
  list, and the literal key-name argument of the `Provider(...)` calls of `transit`
  (its key is read through an attribute, which a literal scan cannot see). A read the
  scan cannot resolve to a literal fails the test rather than being skipped, except two
  known dynamic reads, allowlisted by file in the test with their reason: `main.py`
  (`discover_addons` reads the names of each addon's `required_env`, already in the
  snapshot) and `transit.py` (`Provider.key` reads `key_env`, already in the snapshot
  through `Provider(...)`). Any other dynamic read is a decision, not a blind spot.
Rejected: snapshotting tool *outputs* (they depend on the network and on dates; the
addon tests cover behaviour); a library for snapshots (a JSON file and `==` suffice, and
the failure message prints the differing sections).

### AD-filet-2 — `transit` is tested like its sister addons
Detailed by `rosetta-qualite-filet`. `tests/test_transit.py` follows the other addon
test files: `httpx.MockTransport`, no network. `transit` has no transport hook and `src/`
is not touched, so the test patches `transit.httpx.AsyncClient` with a thin subclass that
injects the mock transport; env set with `monkeypatch`; async tools driven by
`asyncio.run` (no pytest plugin). It covers `_delay_minutes` (equal, later, earlier, missing, malformed),
`Provider.request_kwargs` per provider (SNCF Basic auth, IDFM `apikey` header), the absent-key
message of each provider, station resolution (HTTP error, no result, hit), departures
(status "retardé" / "à l'heure" / "inconnu", error payload) and disruptions (with and
without a station, error payload), exercised through the four public tools.

## Rejected at epic level

- Rewriting addons for style: risk without benefit, contradicts I-4.
- Adding features found missing during the cleanup: out of scope, go to recommendations.

### AD-recommandations-1 — Shape and sources of `docs/recommandations.md`
One entry per finding, each with the same five parts: **Finding** (what is observed and
where: file and line, or workflow step, as of the version audited), **Why it matters**,
**Options** (at least two, one of which is "leave as is" when that is a real choice, each
with its cost and what it changes for the operator), **Recommendation** (one option, with
the reason), **Status** (`open`, then `accepted` or `declined` with the reason, written
later by the operator's decision). The seven subjects named by the slice are mandatory:
personal contacts in default User-Agents, the undocumented `quotas` endpoint, unpinned
`yt-dlp`, base image not pinned by digest, Actions pinned by tag, mypy, splitting
`google.py` and the other large modules. Added to them: every finding carried by the
reviews of the earlier slices (ignored lint findings whose fix would change behaviour,
AD-outillage-1; helper families left un-extracted, AD-nettoyage-2; claims the
documentation could not confirm, AD-doc-1), found in the three ADs named above and in the review notes of the earlier slices (kept by the build
tooling outside this repository; R-8 to R-11 are what they held when this file was written). Each entry carries a cost class for the operator's decision of its
recommended option: S (a text, config or pin change, no code), M (a bounded code change
under the existing tests), L (a refactor or a change of behaviour needing its own slice).
Entries are ordered by that class, S first, within two groups: the seven mandatory
subjects, then the entries added from the earlier slices' reviews (appended after the
seven, never reordering them). The file opens with a table of contents listing id,
subject, cost class and recommended option. The slice writes the file only: no entry is applied, and `src/`, `Dockerfile` and
`.github/` stay untouched (AD-7, I-4). Rejected: opening an issue per finding (a pod
reads only the repo); sorting by severity (nothing here is a defect the epic may fix).

## Decisions of the `rosetta-qualite-nettoyage` slice

Survey of v0.29.0 (the copies are *similar*, rarely *identical*; behaviour preservation
decides which ones can be shared).

### AD-nettoyage-1 — Extract only what is identical, or reproducible by a parameter
A copy is extracted to `_common.py` when every addon that carries it behaves the same,
or when a parameter reproduces each addon's behaviour exactly. A copy that differs in
output (token format, expiry, fallback) stays where it is and is named, with its reason,
in the task review. Rejected: unifying the variants — it changes a visible value
(e.g. OAuth `state` tokens in flight) and contradicts I-4.

### AD-nettoyage-2 — The families, as surveyed
- **HTTP client factory**: `_client()` is `httpx.AsyncClient(timeout=TIMEOUT, transport=_transport)`
  in github, google, maps, marees, meteo, quotas, withings; variants add headers (food),
  redirects (verbatim), a timeout argument (trace), or a split `httpx.Timeout` (git).
  One `http_client(transport, timeout=…, **kw)` in `_common`; each addon keeps its
  `_transport` test hook and a one-line `_client()` that passes it (tests patch
  `addon._transport`).
- **Per-subject file store**: `_read_user`/`_write_user` identical but for the empty value
  (`None` in withings, `{}` in quotas). Extracted with a `default` parameter; the file
  format (`docs/donnees.md`) is read and written byte for byte as before. google and github
  keep their own store (non-atomic or multi-reader paths differ): named, not touched.
- **Signed OAuth `state`**: google and withings are identical (`expiry.sub.sig`, full
  hexdigest, 600 s, key in `state.key`) and are extracted. github's differs (`sub|ts|sig`,
  truncated digest, key derived from the client secret): it **stays**, since unifying
  would invalidate states in flight and change a token.
- **Current subject**: `_current_sub()` is identical in github, quotas and withings, and is
  extracted as `current_subject()` in `_common`; withings also carried a copy of `remote_user`
  (`_remote_user`), now in `_common`. google's `_current_sub()` **stays**: it is not a copy
  (it calls `bridge_learn` and falls back on the identity bridge / the existing file), so
  extracting it would change behaviour.
- **Time zone and paging**: the `TZ` lookups differ in fallback (str / `ZoneInfo` / UTC /
  empty-string handling) and no paging helper is duplicated (the `nextPageToken` match is a
  comment). Not extracted; the review records the check so the family is not re-opened.

### AD-nettoyage-3 — Swallowed exceptions and long functions
The ten `except …: pass|continue` sites of v0.29.0 (postier, courrier, marees, `_common`,
trace, food, verbatim, withings ×3) are each either logged at debug level (no change of
control flow, no new output on the tool path) or kept with `# noqa: S110`/`S112`/`SIM105`
and a reason. The five longest functions at the start (trace_pois 108 lines, marees 105,
mail_attachment 80, wind_forecast 74, then create_app and withings_measures at 71) are cut
by private helpers or kept with their reason; the tool function and its docstring stay.

### AD-outillage-1 — The ruff configuration: a fixed rule set, no wholesale reformatting
`[tool.ruff]` in `pyproject.toml` targets `py312` and selects at least `E, F, W, I, B`
(the ruff default is poorer; an empty selection would make "lint clean" trivial). A finding
is either **fixed** by a change that leaves behaviour untouched (import order, unused
import, a bare `raise` chained with `from`), or **ignored one by one** (`ignore` or
`per-file-ignores`) with a comment giving the reason; a whole rule is never removed from the
set to silence it. A fix that would change behaviour (a default argument, a caught
exception type, an output string) is not made: it is ignored with its reason and the case
is carried to `docs/recommandations.md` (AD-7). `ruff format`
is not run: reformatting the code base would bury the diffs of the cleanup slice. `E501` stays
selected; if the audit shows long lines, `line-length` is raised once, with a comment
giving the measured longest line, rather than ignoring the rule. `ruff` joins the `dev` extra.
Rejected: `select = ["ALL"]` (a lint project of its own); adding `ruff format --check` to CI
(not asked by B-7).

### AD-outillage-2 — Coverage floor measured, then enforced (its CI `test` job is replaced by AD-preuve-1)
`pytest-cov` joins the `dev` extra; `[tool.coverage.run]` has `source = ["src/rosetta"]`
and `[tool.coverage.report]` has `fail_under` set to the **measured total, rounded down
to the integer**, on the full suite (characterisation test and `transit` tests included),
with a comment giving the measured value and date. No target is invented: raising the
floor later is a decision, lowering it is a regression. CI: the workflow keeps its single
`test` job (on which `build` already `needs`); its steps become `pip install -e .[dev]`,
`ruff check .`, `pytest --cov` (the terminal report with missing lines). The commands are
exactly these, so that the README (documentation slice) can quote them. Pull requests already
trigger the workflow (`pull_request: branches: [main]`) and keep doing so. The Actions
keep their tags (AD-7); the workflow is checked by its YAML syntax and its job graph, not
played on the forge.

### AD-doc-1 — The documentation is written from the code and checked mechanically
Every list in the docs has a source the writer re-reads, never memory or `.agent/status.md`:
addons = the modules of `src/rosetta/addons/` not starting with `_`; tools = the tool names
of the characterisation snapshot in `tests/`; environment variables = the variable names
of the same snapshot, each given in the `README.md` configuration table with its meaning,
whether it is required, and its default; state files = the code that reads and writes them
(path from the environment override, format from the serialisation call). The lint and test
commands in the README are copied from `scripts/suite-attestee.sh` (AD-preuve-1; before that `docker-publish.yml`, AD-outillage-2),
not retyped. A claim the code does not confirm is not written; one that cannot be confirmed
is listed in the task review for the recommendations slice (AD-7). `docs/addons.md` has one
section per addon in the same order, each with: purpose, tools, required variables, identity
mode (`machine` or `user`), state files. Rejected: generating the reference from the code at
build time (a build step is a behaviour change to the image, I-4); one doc per addon (a pod
would open fifteen files to learn the hub).

### AD-doc-2 — What survives from `.agent/status.md`
The file is a dated logbook. A passage is **retained** as an `AD-doc-n` entry in
`ARCHITECTURE.md` when it states a choice that constrains future code and is still true
in the code: why an addon works the way it does (one upstream call per model in `meteo`,
the anonymous catalogue window in `auth`, drafts-first sending in `google`, refusal
beyond the supported date range in `marees`, the proxy design of `git`, and similar),
with the rejected alternative when the file gives it. It is **abandoned** when it is a
progress note (ticked boxes, "remaining work", test counts), a deployment or rollout
state, pod-level infrastructure detail, or a decision since reversed by the code. Entries
are written in English, one short paragraph each, grouped under a `## Durable decisions
from the logbook` heading placed after the epic's decisions; a retained decision names the
module it concerns. The task review lists every logbook passage as retained (with its
`AD-doc-n`) or abandoned (with a few words of reason), so the cut can be audited. Then
`.agent/status.md` is deleted (AD-3). Rejected: keeping it under `docs/` (AD-3); carrying
every passage over (it would reproduce the logbook).

## Durable decisions from the logbook

Distilled from `.agent/status.md` (retired, AD-3; it stays in git history). Each entry names
the module it concerns and was re-checked against the code; the cut rule is AD-doc-2.

### AD-doc-3 — `auth`: the anonymous catalogue window is narrow and fails closed
The middleware serves without a token exactly four JSON-RPC methods (`initialize`,
`notifications/initialized`, `ping`, `tools/list`), so that a policy gateway's catalogue
observer, which deliberately holds no hub credential, can watch the tool surface. Every edge
falls back to the 401: `POST` only (the SSE `GET` stays authenticated), mount roots only (one
path segment, so the two-segment routes of `/git/` never match), one well-formed JSON-RPC
object (a batch is refused even when every element is listed; unreadable or over 64 KiB bodies
too), and only when **no** token is presented — an invalid token stays 401. The context
variables stay unset on that path. Accepted risk: tool names and schemas are readable
without a token, on an internal network. Rejected: passive listening (alert too late) and a
dedicated endpoint (code on both sides). The observer must post to the mount (`/<addon>/`),
the hub root has no MCP endpoint.

### AD-doc-4 — `auth`: git gets a Basic envelope and a Basic challenge, nobody else
git cannot send a Bearer header and, faced with a `Bearer` challenge, gives up without asking
its credential helper. `token_from_header` therefore also accepts a Basic header and takes
its password as the token (the token is validated exactly like a Bearer one), and a 401 under
`/git/` answers `Basic realm="rosetta"`. Everywhere else the challenge still points at the
RFC 9728 metadata, so no browser is invited to prompt on an MCP endpoint.

### AD-doc-5 — `auth`: two audiences, and a trusted issuer is not an advertised one
A token is accepted with the hub audience everywhere, or with `<audience>/<addon>` on that
addon's mount only: a token that names its addon is held to it, one that names only the hub
works as before. A second issuer from `ROSETTA_TRUSTED_ISSUERS` (delegation tokens signed by
the policy gateway, `EdDSA`) is trusted for validation and never advertised in the RFC 9728
document: pointing a client at a server where it cannot obtain a token is worse than silence.
The unverified `iss` is read only to select the key set; the signature decides.

### AD-doc-6 — `_common`: the store is keyed on the username, and three forms are normalised
A per-subject store is keyed on `preferred_username` (the only identity the ingress
`Remote-User` gives enrolment), falling back on `sub`; `remote_user` recovers the UTF-8 that
arrives as latin-1 and both go through NFC, because one byte of difference files a credential
under a key no call will match. A delegation token carries only the opaque `sub`: the identity
bridge (`identity_bridge.json`, learnt from any verified token carrying both claims, last write
wins, self-repairing after an IdP storage reset) maps it back. Only `google` is wired to the
bridge. Rejected: keying the store on `sub` (enrolment cannot know it).

### AD-doc-7 — `google`: drafts first; send and delete exist because a gateway arbitrates
Until v0.27 "no sending, no deletion" was structural (no tool). It is now policy: a gateway
in front of the hub decides who may call `mail_send` and `calendar_delete`, on which channel,
after which confirmation. `mail_send` sends a reviewed draft by `draft_id` (the preferred
circuit; the answer reports the recipient re-read from the draft) or composes directly through
the same `_compose` as `mail_draft`, so that drafting and sending cannot fork. Recipient and
text stay chosen by the caller: `mail_send`, like an invitation, is an exfiltration path, said
so in the code. No new scope (`gmail.compose` already covered it). `calendar_delete` reports
an already-deleted event (410) as a state, not a failure. `send_updates` defaults to
`externalOnly`.

### AD-doc-8 — `google`: a truncation must be visible, measurable and recoverable
A cut text states both sizes and names the parameter that raises the limit
(`mail_thread(body_limit=…)`, bounded rather than refused, with a total budget that shrinks
the last messages and never empties one); an opaque cap made an agent guess a threshold and
report a fault. Likewise calendar tools take `calendar_id` (several, or `*`, bounded fan-out),
every event returns the calendar it comes from (`calendar_update` needs it), a failing calendar
is named instead of looking like an empty day, and ids are `quote()`d. Listing calendars needs
its own scope: an older enrolment is told to re-enrol, not given a bare 403.

### AD-doc-9 — `courrier`: the vault arbitrates, drafts only, the box is named
The addon holds no mailbox password: it exchanges the caller's bearer (`current_token`) for a
vault token whose templated policy opens only that caller's credential; the vault decides, not
the addon code. It only searches, reads and drafts (never sends; sending is `postier`'s). Its
tool descriptions name the mailbox they open and say it is not Gmail (and `google` says the
reverse), because two similarly named mail toolsets let an agent report the wrong one as
broken. Rejected: renaming the `MAIL_*` variables (server config, no gain, rollout risk).

### AD-doc-10 — `postier`: the only machine sender is boxed in
Class machine; sender frozen to `POSTIER_FROM`; every recipient must match `POSTIER_ALLOWED`
(no default); a sliding-window hourly quota in process memory; a failed copy to *Sent* is
reported as information, never as a lie about the send.

### AD-doc-11 — `git`: the proxy lets the server judge ancestry, through a scratch ref
The wire protocol carries no force flag and GitHub accepts a force-push on an unprotected
branch, so relaying the pack would protect nothing. A branch update is streamed to a throwaway
ref `refs/heads/rosetta-scratch/<hex>` (which makes the objects exist), then the real ref is
moved by the API with `force: false`, which GitHub refuses natively if it is not a
fast-forward; the scratch ref is deleted in a `finally`. Asking GitHub to compare before the
push was rejected: the pushed commit does not exist yet (404), which refused every update.
Also refused: ref deletion, refs outside `refs/heads/*` and `refs/tags/*`, moving an existing
tag, a push touching several refs, a compressed `receive-pack` body. Only the pkt-line command
prefix is buffered, the pack streams. Upstream, the credential travels as Basic
`x-access-token:<token>` (github.com's smart-HTTP does not accept Bearer). A refusal is an
`ng` report-status line, not an HTTP 403, which git would hide under side-band. Whether a push
*should* happen is the caller's judgement, not the proxy's.

### AD-doc-12 — `github`: a bounded write surface, locked by a test, credential never leaves the hub
Class user, one credential per subject, never shown to the pod, so a shell cannot bypass the
guard. Writes are individually admitted and the absent ones are the guarantee (no deletion of
repositories or branches, no force-push, no secrets or settings): a test pins the tool count
and a list of forbidden words, so an out-of-contract tool breaks the suite. `repo_commit` is
one atomic commit through the Git Data API (`contenu: null` deletes); `repo_create` makes a
new private, empty repository named strictly; `issue_comment` writes without echoing third-party
text and refuses a pull request or a locked issue, so it is not a back door to PR comments; a
pull request merge sends the head sha re-read just before (a moving branch gives 409), re-reads
`mergeable` when GitHub has not computed it yet and says so if it stays `null`, and never
deletes the branch. Titles, bodies and authors of pull requests are third-party text: data to
report, never instructions. A 403 names the missing permission.

### AD-doc-13 — `meteo`: one upstream call per model
A grouped Open-Meteo request drops an out-of-domain model silently and suffixes keys by the
number of survivors, so a naive parser attributes values to the wrong model. One call per
model makes both impossible (the key is always bare, an absence is reported). Null rows past a
model's horizon are dropped, never read as calm; `timezone=auto` gives the spot's local time;
directions average circularly. Free text is geocoded by a commune-only geocoder, hence the
registry first (`ROSETTA_WIND_SPOTS`) and a warning, region and altitude on any guessed place.
No quota helper: the allowance is forty times that of Open Food Facts. Rejected: a grouped
call (cheaper, wrong).

### AD-doc-14 — `marees`: a narrow scope, and refusal over extrapolation
Only times and coefficient. The coefficient is computed for Brest and is the same everywhere;
times vary by port, so answers carry one coefficient per tide and times per port. A date
outside J±30 is refused, a port beyond 100 km is refused (and flagged past 25 km), the source's
attribution (CC BY) and its "unofficial" status are in every answer. The paid official service
was rejected on purpose, not for lack of looking. Places resolve against the known ports first,
then the IGN geocoder (no third key).

### AD-doc-15 — `maps`: the wind comes from the API's own fields, gaps are omitted
Direction is a French cardinal derived from degrees, not a translation of the enum; the hourly
forecast stops at 24 h (the API page size) rather than paginating; a missing scalar is read as
absent or zero according to the proto3 JSON rule (midnight arrives without its `hours` key) and
an unforecast gust is omitted, never 0 km/h. `maps` has a `_transport` hook since it was the
only addon without tests.

### AD-doc-16 — `trace`: geometry never goes through the model
`trace_calcule` returns figures and a URL; `GET /trace/geometrie` returns the polyline, which
the caller writes straight to disk (a few hundred tokens instead of thousands, twice). The
route is stateless — the URL carries the parameters and the track is recomputed — rather than a
cache that would need a lifetime, a size and a replica count. BRouter takes `lon,lat` (the
reverse of the rest of the hub), flipped in a single function; ascent is filtered with a 5 m
hysteresis, one method both ways; Overpass is capped per type, not globally; a waypoint
anchors on the nearest vertex searching forward only (on a loop the last point is the first).
`altimetrie="ign"` is an option, not the default.

### AD-doc-17 — `food` and `verbatim`: read-only, and the source is said
`food` is read-only on purpose (the database is editable by anyone): an agent cannot publish
in a public base. `fields=` is mandatory (a projection shrinks the answer a hundredfold) and
re-renders allergens in English, so the `_tags` are requested and translated here; an absent
product is an answer (`trouve: false`), not a failure; a process-wide `_Quota` queues calls
because the upstream bans the shared egress IP. `verbatim` does no speech recognition: it
fetches published subtitles (json3, then vtt, then srt; a HLS playlist is followed), stitches
them into timed sentences, and returns an empty answer saying so when there are none; `origine`
(`manuel`/`auto`) is in every answer since quoting a machine transcript as the author's words
is a false quote. Rejected: an off-the-shelf stdio MCP for Open Food Facts (the transport this
hub replaces, unbounded surface).

### AD-doc-18 — `withings`, `github`, `quotas`: rotating refresh tokens force a single replica
These addons file a credential that rotates at each refresh: `github` stores the new refresh
token it receives, `withings` and `quotas` also write it atomically before use and refresh
under a per-subject lock; two replicas would burn each other's token.
The lock key is derived the same way as the file name (`_key()` in `quotas`), because one
subject has two spellings (`Sébastien` from a user call, `S_bastien` read back from the file
name) and two locks would stop serialising where both kinds of caller meet. `withings` reads
the real status in the body (every answer is HTTP 200) and keeps measures in `Decimal`
(`value × 10^unit`). The deployment stays at one replica.

### AD-doc-19 — `quotas`: undocumented upstream, read open to machines, enrolment never
There is no public API for a consumer subscription's counters; the addon calls the endpoint
Claude Code's own `/usage` uses, which may change without notice, so it reads the
self-describing `limits[]` array and says "format changed" instead of returning an empty
object; a rate-limited read returns the last figures with their age. Only a credential from a
real login carries the needed scope (a long-lived setup token is refused, 403), so enrolment
takes the refresh token of a login made in a throwaway config directory. Tools sit behind the
JWT (machine tokens included, for supervision pods), `/quotas/enroll` behind the ingress SSO:
no token holder can enrol or replace a credential, and the credential appears in no answer.
Machine calls read the counters of `ROSETTA_QUOTAS_OWNER`; several enrolled users without an
owner is an error, never a guess.

### AD-preuve-1 — The proof of test is an attestation; the image is built only from a tag, after checking it
`rosetta-mcp` applies the chain's general rule R15 (one proof of test per commit, played
wherever it runs best). (1) The suite is declared in `.sdlc.yaml` (`tests:` — `commande`
`scripts/suite-attestee.sh`, `rapport` `resultats.xml`, `format` `junit`, `delai` `15m`; the
suite measured 70 s on a pod, the margin covers a cold virtualenv). The script is what
`docker-publish.yml` ran before and nothing else: build a virtualenv, `pip install -e .[dev]`,
`ruff check .`, `pytest --cov --junitxml=resultats.xml`; the coverage floor of AD-outillage-2
stays in `pyproject.toml` and still fails the run. (2) `attester --publier` plays it on an
extraction of the commit and posts the `tests-complets` status; it runs from a workstation or a
pod of the chain, never from a GitHub workflow. (3) `docker-publish.yml` plays no test and
triggers on `v*` tags (and `workflow_dispatch`, to rebuild a tag attested after it was
pushed; both jobs carry `if: startsWith(github.ref, 'refs/tags/v')`, so a manual run from a
branch builds nothing), never on a branch push, a pull request or a release: an `attestation` job reads the
`tests-complets` status of the tagged commit or of a parent with the same tree (a merge commit
whose content is the attested head's), and `build` needs it. No image is built from `main`:
the `:main` tag disappears from the metadata, `:latest` and the SemVer tags remain, from the
tag. (4) A ruleset `tests-complets` on `main` (public repository) requires the status.
(5) `CLAUDE.md` states the rule. Rejected: keeping `ruff` as a separate CI job (a second proof
of the same commit, which R15 removes); keeping the PR build as a smoke test (the Dockerfile
is exercised at the tag, and a branch push builds nothing); attesting inside the workflow
(the proof would be GitHub's again, not the chain's). The tests themselves are not changed. AD-preuve-1 replaces what AD-6 and AD-outillage-2 say of
CI: no GitHub job plays lint or tests, on pull requests or elsewhere; the verdict a branch gets
is the attestation of its head, and the passages of the README (Development) and
`docs/parcours.md` P-1 that describe the `test` job or a CI verdict are rewritten with it.
