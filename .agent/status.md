# Status

**State.** v0.30.0 plus the quality epic (`rosetta-qualite`): tooling, cleanup and documentation are
delivered; this slice adds `docs/recommandations.md` (R-1 to R-11, all `open`). No recommendation is applied;
`src/`, `Dockerfile` and `.github/` are untouched by it.

**What remains.** The operator decides each entry: write `accepted` or `declined` and the reason in its
**Status** line, then a future slice applies the accepted ones. `tests/test_recommandations.py` pins the shape
of the file (table of contents, five parts per entry, the seven mandatory subjects first).

**Traps still true.**
- A digest or an Action SHA is read on the day of the decision, never copied from the file (R-4, R-5).
- R-1 changes nothing for this deployment until the User-Agent variables are set in its manifest (`k8s-home-lab`).
- New entries go after the last one, keeping the class order within each group.

**Run locally.** `ROSETTA_AUTH=off uvicorn rosetta.main:app --port 8200`, then `curl localhost:8200/health`
(answers `degraded` with no secrets: expected). Tests: `pytest --cov`.
