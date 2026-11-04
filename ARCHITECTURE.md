# Rosetta — architecture

Status of this file: **target for the `rosetta-qualite` epic**. The *Organs* section
describes the code as it is (audited at v0.29.0); `AD-n` entries are decisions, the
first ones taken by the quality epic. Later tranches complete it from the code.

## Invariants

- **I-1** Provider API keys and downstream credentials live server-side only; agents
  hold an identity token and nothing else.
- **I-2** One addon failing never takes the hub or the other addons down.
- **I-3** Adding an addon is adding a module: no change to the hub.
- **I-4** The quality epic changes no behaviour (B-9 in `docs/parcours.md`).

## Organs

- `src/rosetta/main.py` — addon discovery (`pkgutil`, `ROSETTA_ADDONS` allowlist,
  `_`-prefixed modules are helpers), state `ok`/`degraded`/`error`/`disabled`, ASGI app
  assembly (Starlette `Mount` per addon, `/health`, `/`, RFC 9728 metadata), per-addon
  session manager lifespan. The app is built at import (`app = create_app()`).
- `src/rosetta/auth.py` — `BearerJWTMiddleware`: OIDC JWT validation (issuer, audience
  global or `<audience>/<addon>`, trusted issuers), `identity = "user"` enforcement,
  open paths, anonymous catalogue window (`initialize`/`ping`/`tools/list`).
- `src/rosetta/addons/<name>.py` — one FastMCP server per addon; contract: `mcp`,
  optional `required_env`, `identity`, `extra_routes`, `open_paths`.
- `src/rosetta/addons/_common.py` — shared helpers (server factory, HTTP client…).
- Persistence: per-subject files, see `docs/donnees.md`.
- Deployment: multi-stage `Dockerfile`, non-root, port 8200, `HEALTHCHECK /health`;
  image `ghcr.io/antorfr/rosetta-mcp` published by `.github/workflows/docker-publish.yml`;
  manifests live in `AntorFr/k8s-home-lab` (not touched here).

## Decisions

### AD-1 — Scope stays in `rosetta-mcp`
The epic's only target repository is `AntorFr/rosetta-mcp`. `k8s-home-lab` is not a
target: deployment is reserved to the operator and nothing here changes the image
contract. Rejected: opening a `k8s-home-lab` tranche (it would change production for a
no-functional-change epic). Reversed if a documentation fix proves to need a manifest change.

### AD-2 — Documentation layout
`README.md` (what/how to run/configure, operator-facing), `ARCHITECTURE.md` (organs,
`AD-n`, rejections), `docs/addons.md` (per-addon reference), `docs/donnees.md`,
`docs/parcours.md`, `docs/ecrans.md` (stating there is none), `docs/recommandations.md`.
Language: English, as the existing README. Rejected: one giant README (it is already
555 lines and wrong in places); a wiki (a pod reads only the repo).

### AD-3 — `.agent/status.md` is retired
Its lasting decisions are distilled into `ARCHITECTURE.md`; the file is removed and
stays in git history. Rejected: keeping it as `docs/history.md` — 1000 lines of
logbook, with obsolete checkboxes and pod-level infrastructure detail, mislead a pod
more than they inform it.

### AD-4 — "No functional change" is made testable
Before any refactor, a characterisation test pins the tool surface (every addon's tool
names and input schemas, the route list, `/health` shape) and the env variable names
read by the code. Refactors must keep it and the existing 345 tests green. Rejected:
trusting review alone.

### AD-5 — Shared helpers go to `_common.py`, behaviour-preserving
The helpers copied across addons (HTTP client factory, per-user file store, signed
OAuth `state`, current-subject lookup, timezone, paging) are extracted one family per
task, each with the addon tests as the proof. Not touched: public tool signatures,
docstrings that agents read as tool descriptions (they are part of the contract).
Rejected: splitting `google.py` & co. into packages now — a larger diff for no
behavioural gain; listed as a recommendation.

### AD-6 — Tooling: ruff and coverage in CI, mypy recommended only
`pyproject.toml` gets a `ruff` configuration (lint, with the findings of the audit fixed
or explicitly ignored with a reason) and `pytest-cov` in the `dev` extra; CI runs lint
and tests with coverage on pull requests and gates the image build on them. The
coverage floor is set from the measured value (no invented target). mypy is not imposed:
the code base is annotated unevenly, and introducing it is a typing project, kept as a
recommendation. Reversed if a measured first run shows few errors.

### AD-7 — Findings that change behaviour become recommendations
Hard-coded personal contacts in default User-Agents (`contact@antor.fr`), the undocumented
Anthropic endpoint of `quotas`, unpinned `yt-dlp`, base image not pinned by digest,
Actions pinned by tag: each is written in `docs/recommandations.md` with options and a
recommendation. None is applied in this epic, because changing a default or a pin is a
visible change the operator owns.

### AD-filet-1 — The characterisation test reads the code, one snapshot file pins it
Detailed by `rosetta-qualite-filet`. `tests/test_surface.py` builds the surface from the
shipped code and compares it with `tests/surface.json`, a versioned snapshot written once
from v0.29.0 and never regenerated by a refactor (a diff in it is a behaviour change and
is reviewed as one). Four sections, each sorted so the file is stable:
- **tools** — per addon (every module of `rosetta.addons` not starting with `_`), read
  through the FastMCP instance (`await mcp.list_tools()`): name, `description` (the
  docstring agents read, AD-5) and `inputSchema`, verbatim.
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
