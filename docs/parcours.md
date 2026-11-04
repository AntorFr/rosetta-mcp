# Rosetta — who it serves, needs, journeys

Rosetta is a modular MCP hub (see `ARCHITECTURE.md`). This document covers the
**quality epic** `rosetta-qualite`: bringing the repository to the SDLC chain
standard **without any functional change**. Product behaviour is described in
`README.md` and `docs/addons.md`; it is not redesigned here.

## For whom

| Role | Circumstance |
|---|---|
| **Chain pod** (dev, review, verify…) | Takes a feature on Rosetta overnight, with nothing but this repository: no conversation, no operator. |
| **Operator** (Monsieur) | Deploys the image on the home cluster, rotates keys, reads `/health`, decides on improvements. |
| **Agent client** | Calls addon tools over MCP with a Bearer token. Must see **no difference** after this epic. |

## Needs

- **B-1** — A pod can learn what the system is, how it is organised and why, from
  the repository alone: `ARCHITECTURE.md` (organs, decisions `AD-n`, rejected options,
  security, deployment) exists and matches the code.
- **B-2** — `README.md` is true: every addon is listed, the configuration table lists
  every environment variable the code reads, and no statement contradicts the code
  (e.g. "no volume" while three addons persist under `/data`).
- **B-3** — A per-addon reference (`docs/addons.md`: tools, required env, identity
  mode, state file locations) and a data reference (`docs/donnees.md`) exist.
- **B-4** — `.agent/status.md` (a 1000-line logbook) is replaced by durable docs:
  lasting decisions move to `ARCHITECTURE.md`, the rest stays in git history.
- **B-5** — The code is cleaned **behaviour-preservingly**: helpers duplicated across
  addons are shared from `_common.py`, swallowed exceptions are justified or logged,
  lint is clean, the longest functions are split where it clarifies.
- **B-6** — Tests prove it: every addon has a dedicated test file (`transit` has none),
  the tool surface (names and input schemas) is pinned by a characterisation test, and
  coverage is measured.
- **B-7** — CI runs lint and tests with coverage on every pull request, and the image
  is only built if they pass.
- **B-8** — Improvements that would change behaviour or deployment are not done but
  written in `docs/recommandations.md`, each with options and a recommendation, for
  the operator to arbitrate.
- **B-9** — **Invariant**: no functional change. Existing tests pass unmodified (a test
  may be *added* or moved, not weakened), tool names/schemas, HTTP routes, env
  variable names and defaults, and the image entrypoint are unchanged.

## Journeys

- **P-1 — A pod takes a feature on Rosetta** (B-1, B-2, B-3, B-6, B-7). Reads
  `README.md` → `ARCHITECTURE.md` → `docs/addons.md` for the addon at stake; writes the
  addon change and its test; runs lint and tests with the documented commands; opens a
  PR; CI gives the same verdict.
- **P-2 — The operator reviews quality and decides** (B-8, B-9). Reads the epic's
  result and `docs/recommandations.md`, then arbitrates each recommendation
  (accept → a future feature, reject → recorded).
- **P-3 — An agent client calls a tool** (B-9). Nothing changes: this journey is the
  regression guard, proven by the schema snapshot and the unchanged test suite.

## Out of scope

- Any new addon, any functional evolution, any change of default value or of a tool
  contract (these go to `docs/recommandations.md`).
- The `vin-lwin` branch (unmerged vin/LWIN addon, `d0646c8`): left aside, to be taken as
  a separate feature.
- Deployment manifests in `AntorFr/k8s-home-lab` (`clusters/tantive/home/mcp/`):
  reserved to the operator; deployment-side findings go to the recommendations.
- Type-checking with mypy: recommended, not imposed (see AD-6).
