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

### AD-outillage-1 — The ruff configuration: a fixed rule set, no wholesale reformatting
`[tool.ruff]` in `pyproject.toml` targets `py312` and selects at least `E, F, W, I, B`
(the ruff default is poorer; an empty selection would make "lint clean" trivial). A finding
is either **fixed** by a change that leaves behaviour untouched (import order, unused
import, a bare `raise` chained with `from`), or **ignored one by one** (`ignore` or
`per-file-ignores`) with a comment giving the reason; a whole rule is never removed from the
set to silence it. A fix that would change behaviour (a default argument, a caught
exception type, an output string) is not made: it is ignored with its reason and the case
is carried to `docs/recommandations.md` by the recommendations slice (AD-7). `ruff format`
is not run: reformatting the code base would bury the diffs of the cleanup slice. `E501` stays
selected; if the audit shows long lines, `line-length` is raised once, with a comment
giving the measured longest line, rather than ignoring the rule. `ruff` joins the `dev` extra.
Rejected: `select = ["ALL"]` (a lint project of its own); adding `ruff format --check` to CI
(not asked by B-7).

### AD-outillage-2 — Coverage floor measured, then enforced in the existing `test` job
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

