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

## Rejected at epic level

- Rewriting addons for style: risk without benefit, contradicts I-4.
- Adding features found missing during the cleanup: out of scope, go to recommendations.

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
- **Current subject**: `_current_sub()` is identical in github, google, quotas, withings;
  withings also carries a copy of `remote_user` (`_remote_user`), now in `_common`.
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
