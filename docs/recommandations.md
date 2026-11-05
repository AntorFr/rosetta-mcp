# Rosetta — recommendations

Findings of the quality epic that would change behaviour, a pin or a default, so none was applied (AD-7, I-4). Each is written for the operator to decide. State of the code audited: v0.30.0 plus the epic's slices (`pyproject.toml`, `Dockerfile`, `.github/workflows/docker-publish.yml`, `src/rosetta/addons/`).

Cost classes (AD-recommandations-1): **S** a text, config or pin change, no code; **M** a bounded code change under the existing tests; **L** a refactor or a change of behaviour needing its own slice. Entries are in two groups, the seven first subjects (R-1 to R-7) then the findings added from the earlier slices (R-8 onward), each ordered S, then M, then L; a new entry goes at the end of the last group, keeping that order. Status is `open` until the operator decides (`accepted` or `declined`, with the reason).

## Table of contents

| Id | Subject | Class | Recommended option |
|---|---|---|---|
| R-1 | Personal contacts in default User-Agents | S | B — neutral default, contact supplied by the operator |
| R-2 | Undocumented Anthropic endpoint of `quotas` | S | A — keep, and state the risk in the README |
| R-3 | `yt-dlp` not pinned | S | A — keep the floor, rebuild on a schedule |
| R-4 | Base image not pinned by digest | S | B — pin `python:3.12-slim` by digest, bump by hand |
| R-5 | Actions pinned by tag | S | B — pin each Action to a commit SHA, keep the tag in a comment |
| R-6 | mypy | M | B — add mypy in CI with a baseline of the current errors |
| R-7 | Splitting `google.py` and the other large modules | L | B — split `google.py` only, in its own slice |
| R-8 | `google` and `github` write their credential file in place, not atomically | M | B — write through `write_user` (temporary file, then `os.replace`) |
| R-9 | `github` OAuth `state` has its own format and key | M | A — leave as is, unify only with a planned re-enrolment window |
| R-10 | `google._current_sub` kept apart from `current_subject` | M | A — leave as is, it is not a copy |
| R-11 | Five different fallbacks for the house time zone | M | B — one `house_tz()` helper, after the operator has chosen the fallback |

## R-1 — Personal contacts in default User-Agents

**Finding.** Two defaults embed a personal address: `src/rosetta/addons/food.py:68` (`OFF_USER_AGENT`, default `Alfred/1.0 (contact@antor.fr)`) and `src/rosetta/addons/trace.py:554-555` (`OVERPASS_USER_AGENT`, default `rosetta-mcp/trace (contact@antor.fr)`). Both are documented as defaults in the `README.md` configuration table and in `docs/addons.md`. Other addons send a neutral name (`UA = "rosetta-github-addon"` in `github.py:76`, `"rosetta-git-proxy"` in `git.py:53`); `quotas.py:135` sends `claude-code/2.1.274`, which is a different subject (R-2).

**Why it matters.** Anyone who runs the published image sends the maintainer's address to Open Food Facts and to Overpass. Those services ask for a contact in the User-Agent so they can reach the caller; with this default, they reach the wrong person, and the address is in a public repository.

**Options.**
- A. Leave as is. Cost: none. Effect: unchanged; the operator of this deployment is the maintainer.
- B. Neutral default (`rosetta-mcp/food`, `rosetta-mcp/trace`), the operator sets the variable with their own contact. Cost: S (two literals, two README lines, the addon docs); effect: the contact disappears from requests of any deployment that has not set the variable, including this one until it is set in its manifest (`k8s-home-lab`, out of this repository). Open Food Facts and Overpass may rate-limit an anonymous name more strictly.
- C. Refuse to start the addon without the variable. Cost: M (code, and a test that today boots without it); effect: a deployment that did not set it loses `food` and `trace`.

**Recommendation.** B: set the variable in the deployment manifest first, then change the defaults. The anonymity gain is real and the effect on the maintainer's own deployment is nil once the manifest is updated.

**Status.** `open`

## R-2 — Undocumented Anthropic endpoint of `quotas`

**Finding.** `src/rosetta/addons/quotas.py:125-128` calls `https://api.anthropic.com/api/oauth/usage`, `/api/oauth/profile` and `/v1/oauth/token`: the endpoint Claude Code's own `/usage` uses, with no public documentation. The call only works with a `claude-code/<version>` User-Agent (`quotas.py:135`, default `claude-code/2.1.274`, the module header explains it answers 429 otherwise) and with the `anthropic-beta` header (`quotas.py:357`). The addon already reads the self-describing `limits[]` array and answers "format changed" instead of an empty object (AD-doc-19).

**Why it matters.** The endpoint, the beta header or the accepted User-Agent can change without notice, and the addon passes itself off as another client by design. The failure is visible (an error answer), not silent, but nothing tells the operator ahead of time.

**Options.**
- A. Leave as is and state in the README that `quotas` depends on an undocumented endpoint and may stop working. Cost: S (one line). Effect: none on behaviour.
- B. Remove the addon from the default set (opt-in through a setting). Cost: M (a setting and a test of the catalogue; `tests/surface.json` changes). Effect: deployments lose `quotas` until they opt in.
- C. Drop the addon. Cost: L (removal of a feature and its enrolment page); effect: no supervision of the subscription counters.

**Recommendation.** A. The behaviour is already defensive and the feature is wanted; the missing piece is saying so to the reader.

**Status.** `open`

## R-3 — `yt-dlp` not pinned

**Finding.** `pyproject.toml:30` declares `"yt-dlp>=2025.1"`, a floor with no ceiling and no lock file; the comment above it (lines 26-29) says it is deliberate. The `Dockerfile` (`pip install .` in the build stage, line 12) takes whatever is newest when the image is built: 2026.10.26 at the time of this audit. The addon imports it lazily (`verbatim.py:93`) and answers "yt-dlp n'est pas installé" when it is absent (`verbatim.py:114`). `verbatim` tests mock `_extract`, so no test exercises the real library.

**Why it matters.** Two risks pull opposite ways. An old `yt-dlp` stops working on every video when a site changes its player; a new one can change the shape of `info` and break `verbatim` at the next build, with no test to say so. Reproducibility of the image is also lost: two builds of the same commit differ.

**Options.**
- A. Keep the floor and make rebuilds regular (a scheduled workflow, or a rebuild when the operator sees `verbatim` fail). Cost: S. Effect: the image stays current; a breaking release arrives with the rebuild.
- B. Pin an exact version (`yt-dlp==2026.10.26`) and bump it on purpose. Cost: S (one line); effect: reproducible builds, but `verbatim` ages silently until someone bumps it, which is the failure the comment warns about.
- C. Pin a compatible range (`>=2026.10,<2027`). Cost: S; effect: bounds a breaking major, but the project's versions are dates, so the ceiling means little.

**Recommendation.** A: the maintenance the comment describes is "rebuild", and a scheduled rebuild is a workflow change (see R-5 for the pinning of what that workflow uses). Revisit if a rebuild ever breaks `verbatim`.

**Status.** `open`

## R-4 — Base image not pinned by digest

**Finding.** `Dockerfile:6` and `Dockerfile:15` both use `FROM python:3.12-slim`: a moving tag, for the build stage and for the runtime. The workflow builds with `docker/build-push-action@v6` (cache `type=gha`) on every push, so each build resolves the tag again.

**Why it matters.** The tag moves with each Python 3.12 patch and each Debian rebuild: the same commit gives different images, and a faulty upstream image reaches production at the next build. Pinning by digest makes the build reproducible and the change deliberate; it also means security patches of the base no longer arrive by themselves.

**Options.**
- A. Leave as is. Cost: none. Effect: the base stays current, builds stay unreproducible.
- B. `FROM python:3.12-slim@sha256:<digest>` in both stages, with the tag kept for reading, bumped by hand when the operator wants a newer base. Cost: S (two lines, and the digest read from the registry at the time of the decision). Effect: reproducible; base patches wait for a bump.
- C. B plus an update bot (Dependabot or Renovate) that proposes the bump. Cost: S for the config but the bot opens pull requests the operator must read; effect: pinned and current.

**Recommendation.** B now, C when the operator wants the bumps to come to them. The digest is not written here: it must be read from the registry on the day the option is accepted, or it will be stale on the day it is applied.

**Status.** `open`

## R-5 — Actions pinned by tag

**Finding.** `.github/workflows/docker-publish.yml` uses seven Actions by major tag: `actions/checkout@v4` (lines 25 and 37), `actions/setup-python@v5` (26), `docker/setup-qemu-action@v3` (39), `docker/setup-buildx-action@v3` (40), `docker/login-action@v3` (44), `docker/metadata-action@v5` (52), `docker/build-push-action@v6` (64). The `GITHUB_TOKEN` of the `build` job has `packages: write` (workflow `permissions`, lines 13-15) and the job pushes the production image.

**Why it matters.** A tag can be moved: a compromised or mistaken release of one Action runs with the token that pushes the image. A commit SHA cannot be moved.

**Options.**
- A. Leave as is. Cost: none. Effect: Actions stay current inside their major.
- B. Pin each Action to a full commit SHA, keeping the tag in a trailing comment. Cost: S (seven lines; the SHAs read from each repository on the day of the decision). Effect: immutable; bumps are manual.
- C. B plus Dependabot for `github-actions`. Cost: S for the config; effect: bumps come as pull requests.
- D. Pin only the third-party `docker/*` Actions and leave `actions/*` (GitHub's own) by tag. Cost: S (five lines); effect: covers the less trusted owner, accepts GitHub's.

**Recommendation.** B, with C if the operator wants the bumps proposed. D is the minimum if the effort has to be limited.

**Status.** `open`

## R-6 — mypy

**Finding.** No type checker is configured: `pyproject.toml` has `[tool.ruff]` and `[tool.coverage]` but no `[tool.mypy]`, and the workflow runs `ruff check .` and `pytest --cov` only (AD-6 leaves mypy as a recommendation). Measured with mypy 2.4.1 on `src/` (19 files), default settings, with the package installed: **25 errors in 11 files**; with `--strict`: **418 errors in 17 files**. The default-run errors are of a few kinds, e.g. `courrier.py:390` and `:404` (`"object" has no attribute "__iter__"`), `git.py:374` (annotation needed for `commands`), plus `arg-type` errors in `github.py` and `assignment` errors in `marees.py`. `yt_dlp` has no stubs (`verbatim.py:93` warns on import).

**Why it matters.** The annotations are uneven: some serve as documentation, some are wrong or missing, and nothing checks them. The default run shows a few places where a type is untrue (a possible latent bug, not proven one).

**Options.**
- A. Leave as is. Cost: none. Effect: no check; the annotations drift.
- B. Add mypy (default mode) to the `dev` extra and to the `test` job, with the 25 current errors fixed or ignored one by one with a reason, as AD-outillage-1 did for ruff. Cost: M (code touched in 11 files under the existing tests; a change of type in a few places, to be checked for behaviour). Effect: CI refuses new type errors.
- C. `--strict` with the 418 errors. Cost: L (a typing project). Effect: full coverage; far outside the epic's constraints.
- D. Advisory job (mypy runs, failures do not gate). Cost: S; effect: the number is visible, nobody is blocked, and an advisory job tends to be ignored.

**Recommendation.** B. The measured count is small enough to settle in one bounded change, and the AD-6 condition ("reversed if a measured first run shows few errors") is met by it. Each fix must keep the behaviour (I-4); a fix that changes it is a separate decision.

**Status.** `open`

## R-7 — Splitting `google.py` and the other large modules

**Finding.** Sizes of `src/rosetta/addons/` (lines): `google.py` 1376, `github.py` 833, `withings.py` 806, `quotas.py` 742, `trace.py` 721, `verbatim.py` 530, `meteo.py` 509. `google.py` has 45 top-level functions and five banner sections: credential store (line 109), Gmail helpers (219), Gmail tools (362), Calendar tools (850) and the browser enrolment (1294). Its tools are registered on one `FastMCP` instance at import time; `tests/test_google.py` is the largest test file and `tests/surface.json` pins the tool names and docstrings. AD-nettoyage-3 cut long functions but left module boundaries alone.

**Why it matters.** A reader looking for the Calendar code reads a 1376-line file; a change to the enrolment page sits in the same diff as one to a Gmail tool. The size itself is not a defect, and the module works; the cost is in reading and review.

**Options.**
- A. Leave as is. Cost: none. Effect: none.
- B. Split `google.py` into a package (`google/` with the store, Gmail, Calendar, enrolment), the tool names, the mount path `/google` and `tests/surface.json` unchanged. Cost: L (import paths used by `tests/test_google.py` and by any patch of module attributes such as `_transport` change; the characterisation test must stay green without edits). Effect: no visible change to clients.
- C. Split all seven modules the same way. Cost: L, several slices; effect: uniform, but the other modules are 500-830 lines and have a single concern each.
- D. Only extract the enrolment page (about 80 lines to the end of the file) into a private module. Cost: M; effect: small gain, no change of the import path of the tools.

**Recommendation.** B, in its own slice, because tests patch module attributes and the split cannot be done without changing how they reach them (a decision for the tester, since existing tests may not be edited by the author). D is the cheap step if the operator wants to start smaller. C is not recommended: nothing in the other modules asks for it.

**Status.** `open`

## R-8 — `google` and `github` write their credential file in place

**Finding.** `withings` and `quotas` write their per-subject file through `write_user` (`src/rosetta/addons/_common.py:209-218`: temporary file, `chmod 600`, `os.replace`). `google` and `github` do not (AD-nettoyage-2, "per-file store", left un-extracted): `google.py:1355-1363` (enrolment callback) and `github.py:157-159` (rotation of the refresh token on each refresh) and `github.py:821-825` (enrolment) open the final path with `"w"`, `json.dump`, then `os.chmod(path, 0o600)`. The file is therefore truncated before it is rewritten, and for a moment it has the default mode of the process rather than 0600.

**Why it matters.** GitHub rotates the refresh token at every use (AD-doc-18): a crash or a kill between the truncation and the end of the dump leaves an empty file and the user must enrol again. The window is short, but it is the one case where the file is the only copy of a secret that cannot be re-read from upstream. The brief mode window is a minor point, on a data volume only the process reads.

**Options.**
- A. Leave as is. Cost: none. Effect: unchanged; the risk stays a rare one.
- B. Write through `write_user(path, record)` in `github` (refresh and enrolment) and in `google` (enrolment). Cost: M (three call sites, no change of format: `write_user` makes the same `json.dump` bytes and mode 0600, checked by the `nettoyage-magasin` review; `google` and `github` read the file in their own way, which does not change). Effect: no visible change; a crash leaves the old or the new file.
- C. B plus one shared reader (`read_user`) for all four addons. Cost: M-L (the readers differ in what they do on a corrupt file and in the default value, so each one needs its own characterisation first). Effect: one store; larger diff.

**Recommendation.** B, with the existing tests: nothing the tools return changes. C only if the operator wants the readers unified as well; it is not needed for the risk above.

**Status.** `open`

## R-9 — `github` OAuth `state` has its own format and key

**Finding.** `google` and `withings` sign the OAuth `state` through `_common.state_codec` (`expiry.sub.sig`, full hexdigest, key in `state.key`; `google.py:1298`, `withings.py:726`). `github` keeps its own (AD-nettoyage-2, "signed OAuth state", left un-extracted): `github.py:747-770` builds `sub|ts|sig` in base64, with a digest truncated to 32 characters and a key derived from the OAuth client secret (`_state_key`, `github.py:747-750`, falling back on the constant `"rosetta-github"` when no client is configured).

**Why it matters.** Two formats and two key sources for the same job. The GitHub key depends on the client secret, so rotating the secret invalidates the states in flight (a ten-minute window), and with no client configured the key is a constant public in the repository (the state then proves nothing, but enrolment needs the client anyway). Unifying would invalidate every state in flight at the moment of the deploy, which is a visible change; that is why it was not done in the cleanup.

**Options.**
- A. Leave as is. Cost: none. Effect: unchanged.
- B. Move `github` to `state_codec`. Cost: M (one line to wire, plus a check that the tests which call `_sign_state` / `_verify_state` of `github` still reach them: they are existing tests, to be reviewed by the tester). Effect: enrolments started before the deploy and ended after it fail once ("state invalide"), the user restarts from `/github/enroll`; the key now lives in `state.key` and no longer follows the client secret.
- C. B, deployed with a short-lived acceptance of the old format. Cost: M-L (a second verifier to write and remove later). Effect: no failed enrolment; more code for ten minutes of benefit.

**Recommendation.** A. The two formats work; the only gain from B is uniformity and the cost is a failed enrolment at the deploy. If the operator does want one codec, B at a moment when nobody is enrolling.

**Status.** `open`

## R-10 — `google._current_sub` kept apart from `current_subject`

**Finding.** `github`, `quotas` and `withings` read the caller from `_common.current_subject`. `google` keeps its own `_current_sub` (`google.py:141-165`; AD-nettoyage-2, "current subject", left un-extracted; the arbitration of `rosetta-qualite-nettoyage-sujet` recorded it): it prefers `preferred_username` (NFC-normalised), teaches the identity bridge (`bridge_learn`), then falls back on the bare `sub` if a file exists under it, then on what the bridge resolved, then on the bare `sub`.

**Why it matters.** A reader sees four versions of "who is calling" and has to know why one differs. The difference is real (the bridge, AD-doc-6): it is not duplicated code but a different rule, so the cost is one of reading, not of correctness. The risk of sharing is on the other side: a parameter that reproduces the bridge inside `_common` makes the common helper depend on `google`'s file layout.

**Options.**
- A. Leave as is, with its reason in `docs/addons.md` or a comment above the function. Cost: S-M (a comment; no code change). Effect: none.
- B. `current_subject(…, resolver=…)` in `_common`, with `google` passing its bridge rule. Cost: M (the existing tests of the bridge and of `google` must pass without edits; the order of `bridge_learn` before the file test is observable). Effect: one entry point, a callback with `google`'s whole rule in it, hence little gain.

**Recommendation.** A: it is not a copy. Revisit only if a fifth addon needs the bridge.

**Status.** `open`

## R-11 — Five different fallbacks for the house time zone

**Finding.** Every addon that needs the house zone reads `TZ` and falls back on its own: `google.py:858` `os.environ.get("TZ", DEFAULT_TZ)` (an empty `TZ` stays empty and is sent as `timeZone: ""`); `marees.py:331` the same with a literal `"Europe/Paris"`; `meteo.py:114` `TZ or "Europe/Paris"` (empty gives Paris, an unknown name is passed on as a string); `quotas.py:404-406` `ZoneInfo(TZ or DEFAULT_TZ)` (an invalid name gives Paris); `withings.py:363-371` `TZ`, then `DEFAULT_TZ`, then UTC (AD-nettoyage-2, "time zone and paging", left un-extracted; the `nettoyage-sujet` review listed the five). `DEFAULT_TZ = "Europe/Paris"` is declared three times (`google.py:99`, `quotas.py:137`, `withings.py:78`). No paging helper is duplicated, so that part of the family needs no entry.

**Why it matters.** With `TZ` unset or valid nothing differs. The differences show with an empty or an invalid `TZ`: `google` and `marees` send an empty zone to their upstream, `withings` answers in UTC, `quotas` and `meteo` in Paris. A deployment with a typo in `TZ` gets different, partly wrong, results per addon.

**Options.**
- A. Leave as is. Cost: none. Effect: unchanged; the case needs a misconfiguration.
- B. One `house_tz()` in `_common` (empty or unknown gives `Europe/Paris`, or UTC, as the operator chooses) used by the five. Cost: M (five call sites, tests of each addon to keep green; `withings` currently differs by ending on UTC, so the choice is made once and a test pins it). Effect: an empty or invalid `TZ` now gives the same zone everywhere, which changes the answer of `google`, `marees` and `withings` in that case only.
- C. Validate `TZ` at start-up and refuse to start on an unknown name. Cost: M (code in `main`, and a test that boots with a bad `TZ`); effect: a misconfiguration is seen at deploy time; a deployment that has one stops.

**Recommendation.** B, choosing Paris as the single fallback (it is the default the code already names in four places; UTC in `withings` only guards against a missing tz database, which `tzdata` in `pyproject.toml` now removes). Decision first, code after.

**Status.** `open`
