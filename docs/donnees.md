# Rosetta — data

Rosetta has no database. Its state is a set of **per-subject files** written by the
user-data addons, under a volume. This epic changes none of them.

| Store | Env override | Default path | Written by | Content |
|---|---|---|---|---|
| Google credentials | `ROSETTA_GOOGLE_DATA` | `/data/google` | `google` | OAuth grant per username, identity bridge |
| Withings credentials | `ROSETTA_WITHINGS_DATA` | `/data/withings` | `withings` | OAuth grant per username |
| Quotas credentials | `ROSETTA_QUOTAS_DATA` | `/data/quotas` | `quotas` | Claude credential per username |
| GitHub data | `ROSETTA_GITHUB_DATA` | `/data/github` | `github` | per-subject enrolment |

Rules: one file per subject, keyed on `preferred_username`; a verified token carrying
both username and IdP `sub` teaches an identity bridge so a `sub`-only token resolves
the same file; machine tokens are refused (403) on `identity = "user"` addons. The
exact file formats are documented per addon in `docs/addons.md` (to be written by the
documentation tranche from the code).
