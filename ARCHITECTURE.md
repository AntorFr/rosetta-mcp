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
documentation could not confirm, AD-doc-1), found in the three ADs named (on the
slices' branches until they are merged into the epic) and in the review lines of the
slices' tasks (`tasks/<id>/discussion.md` of the pilotage store). Each entry carries a cost class for the operator's decision of its
recommended option: S (a text, config or pin change, no code), M (a bounded code change
under the existing tests), L (a refactor or a change of behaviour needing its own slice).
Entries are ordered by that class, S first, within two groups: the seven mandatory
subjects, then the entries added from the earlier slices' reviews (appended after the
seven, never reordering them). The file opens with a table of contents listing id,
subject, cost class and recommended option. The slice writes the file only: no entry is applied, and `src/`, `Dockerfile` and
`.github/` stay untouched (AD-7, I-4). Rejected: opening an issue per finding (a pod
reads only the repo); sorting by severity (nothing here is a defect the epic may fix).
