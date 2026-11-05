# Rosetta — data

Rosetta has no database. Its state is a set of **per-subject files** written by the
user-data addons, under a volume. This epic changes none of them. Everything below is
read from the code (`src/rosetta/addons/_common.py` and the four addons); a field the
code does not write is not listed.

| Store | Env override | Default path | Written by | Content |
|---|---|---|---|---|
| Google credentials | `ROSETTA_GOOGLE_DATA` | `/data/google` | `google` | OAuth grant per username, identity bridge |
| Withings credentials | `ROSETTA_WITHINGS_DATA` | `/data/withings` | `withings` | OAuth grant per username |
| Quotas credentials | `ROSETTA_QUOTAS_DATA` | `/data/quotas` | `quotas` | Claude credential per username |
| GitHub data | `ROSETTA_GITHUB_DATA` | `/data/github` | `github` | per-subject enrolment |

Rules: one file per subject, keyed on `preferred_username`; a verified token carrying
both username and IdP `sub` teaches an identity bridge so a `sub`-only token resolves
the same file; machine tokens are refused (403) on `identity = "user"` addons.

## Common to the four stores

- **File name**: `<data dir>/users/<safe>.json`, where `<safe>` is the subject with every
  character outside `[A-Za-z0-9_.-]` replaced by `_`, cut to 64 characters. The accents are
  therefore lost in the name; the real spelling is in the `sub` key of the record.
- **Encoding**: one JSON object, written with `json.dump` (no indentation).
- **Permissions**: file mode `0600`. `google`, `withings` and `quotas` write through
  `write_user` (`_common.py`): a `<file>.tmp` is written, chmod'ed, then `os.replace`d
  (atomic). `github` writes the file directly (`open(..., "w")` then `chmod 600`), not atomically.
- **Keys**: all four records have `sub` (the subject, string), `refresh_token` (string) and
  `enrolled_at` (integer, epoch seconds) — in `quotas`, `refresh_token` and `enrolled_at` sit
  inside `providers.claude`. When a refresh rotates the token, `refreshed_at`
  (integer, epoch seconds) is added next to it (`github`, `withings`, `quotas`; not `google`,
  which never rewrites its file on refresh).
- **Missing or unreadable file** (`read_user`): `google`/`withings` see no enrolment
  (`None`), `quotas` sees `{}`; the tool answers with the enrolment URL.

## Google — `ROSETTA_GOOGLE_DATA` (`/data/google`)

| File | Content |
|---|---|
| `users/<safe>.json` | `{"sub": str, "refresh_token": str, "scopes": [str, …], "enrolled_at": int}` — `scopes` is the space-separated `scope` of the token response, split |
| `client_secret.json` | provisioned by the operator, never written by Rosetta: Google's OAuth client file; the `web` or `installed` object (or the top level) must have `client_id` and `client_secret` |
| `identity_bridge.json` | `{"<opaque sub>": "<username>", …}` — written with `indent=1`, atomic; last write wins; tolerant read (missing or mangled = empty bridge) |
| `state.key` | 32 random bytes (binary), mode `0600`, created on first use: the HMAC key of the OAuth `state` |

## Withings — `ROSETTA_WITHINGS_DATA` (`/data/withings`)

| File | Content |
|---|---|
| `users/<safe>.json` | `{"sub": str, "userid": <Withings user id>, "refresh_token": str, "scopes": [str, …], "enrolled_at": int}`, plus `refreshed_at` after a rotation (`userid` may also be updated then) |
| `state.key` | as for Google |

The OAuth client (`WITHINGS_CLIENT_ID`, `WITHINGS_CLIENT_SECRET`) is in the environment, not in a file.

## Quotas — `ROSETTA_QUOTAS_DATA` (`/data/quotas`)

| File | Content |
|---|---|
| `users/<safe>.json` | `{"sub": str, "providers": {"claude": {"refresh_token": str, "enrolled_at": int, "refreshed_at": int}}}` — `refreshed_at` only after a rotation |

`providers` is keyed by provider name (only `claude` exists today). The enrolled subjects are
listed from the file names (`<name>.json` minus the extension), so a machine call with one
enrolled subject reads that one (see `ROSETTA_QUOTAS_OWNER` in `docs/addons.md`). Enrolment
stores the token, probes the usage endpoint, and puts the previous content back if the probe fails.

## GitHub — `ROSETTA_GITHUB_DATA` (`/data/github`)

| File | Content |
|---|---|
| `users/<safe>.json` | `{"sub": str, "refresh_token": str, "enrolled_at": int}`, plus `refreshed_at` after a rotation (GitHub rotates the refresh token at every use and the file is rewritten before the access token is returned) |

No `state.key` file: the `state` of the GitHub OAuth flow is signed with a key derived from
`GITHUB_CLIENT_SECRET` (a different format from `google`/`withings`).

## OAuth `state` (not stored)

`google` and `withings`: `base64url(expiry.sub.sig)`, SHA-256 hexdigest, valid 600 s, key in `state.key`.
`github`: `base64url(sub|timestamp|sig)`, digest truncated to 32 hex characters, valid 600 s.
Neither is persisted; changing either format invalidates the states in flight.
