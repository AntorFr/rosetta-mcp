"""`quotas` addon - how much of a subscription is left, user-data class, READ-ONLY.

Contract (one tool per question, and the guard IS the tool surface - nothing
here spends, buys or changes anything):
  - quotas               : les compteurs d'un abonnement (fenêtres, plafonds, crédits)
  - quotas_fournisseurs  : quels fournisseurs cet addon sait lire, et lesquels sont enrôlés

Identity: READING is open to machine tokens (a supervision pod watching the
gauges has no human behind it), ENROLMENT is not. They are guarded by different
layers, which is what makes the split safe rather than a hole:

  - the tools are reached with the hub's JWT, and accept a machine subject;
  - `/quotas/enroll` is exempt from that JWT (`open_paths`) and guarded by the
    ingress SSO instead - it refuses anything without a `Remote-User` header,
    so no bearer token, machine or not, can enrol or replace a credential.

WHAT A STOLEN MACHINE TOKEN BUYS, THEREFORE, IS A READING - AND ONLY A READING.
This addon exposes no tool that sends a prompt, and its entire outbound surface
to Anthropic is one GET on the usage endpoint plus the token refresh that
precedes it: `/v1/messages` is never called from here. The stored credential is
a FULL account credential, so it is never returned, never logged, and never
rendered into an answer or an error. A test pins all three - the guarantee is
the tool surface, and a surface is worth pinning.

Whose gauges a machine call reads is `ROSETTA_QUOTAS_OWNER`, or the single
enrolled subject when there is exactly one. Several enrolled and no owner set
is an ERROR naming the variable, never a guess: picking a household member's
personal budget by alphabetical order is not a default, it is a leak.
`ROSETTA_QUOTAS_CLIENTS` narrows further, to named machine subjects.

Today one provider: **Claude** (claude.ai subscription - Pro, Max, Team,
Enterprise). The registry below is the extension point; a second provider is a
dict entry plus its two functions, not a second addon.

────────────────────────────────────────────────────────────────────────────
⚠️ FOUR facts measured against the live service on 2026-09-17, not read in a
doc - because there is no doc. All four cost an afternoon if ignored.

 1. THERE IS NO PUBLIC API FOR THIS. Anthropic's documented Usage & Cost Admin
    API reports the token spend of a *Console organization* and states it is
    "unavailable for individual accounts". It says nothing about a claude.ai
    subscription's session and weekly windows. The endpoint used here,
    `GET /api/oauth/usage`, is the one Claude Code's own `/usage` calls. It is
    undocumented: it will change or disappear. Hence the rule this module obeys
    everywhere - when the shape stops matching, SAY SO; never serve a number
    whose meaning is no longer certain.

 2. A `claude setup-token` CREDENTIAL CANNOT READ IT. That one-year OAuth token
    is the officially documented way to authenticate a subscription outside a
    browser, and it is the obvious thing to reach for. It is refused:

        403  {"error_code": "oauth_scope_insufficient",
              "required_scopes": ["user:profile"]}

    while the very same token gets a 200 on /v1/messages. The documentation's
    "it can only make model requests" is literal. Only a credential minted by a
    real `/login` carries `user:profile`. Hence the enrolment below asks for a
    LOGIN's refresh token, and hence it refuses anything that fails the probe.

 3. THE USER-AGENT IS LOAD-BEARING. Without a `claude-code/<version>` User-Agent
    the request lands in an anonymous, aggressively rate-limited bucket and
    answers 429 - even with a perfectly valid token. This is not cosmetic
    politeness; it is the difference between data and a permanent error.

 4. A SECOND LOGIN DOES NOT REVOKE THE FIRST. The credential enrolled here is
    meant to be minted in a throwaway config directory (see the enrolment page),
    so the hub holds a credential of its own and never touches the one in the
    operator's keychain. Verified: both were live simultaneously.

────────────────────────────────────────────────────────────────────────────
Two shaping decisions worth knowing before reading the code:

  - THE ANSWER IS BUILT FROM `limits[]`, NOT FROM THE TOP-LEVEL KEYS. The
    payload carries both: a flat set of named buckets (`five_hour`, `seven_day`,
    `seven_day_opus`, plus a dozen codenames for unreleased features -
    `nimbus_quill`, `cinder_cove`, `copper_kite`… - all null on a normal
    account), and a self-describing `limits` array whose entries name their own
    `kind`, `percent`, `scope` and `resets_at`. Reading the array means a bucket
    Anthropic adds tomorrow shows up on its own instead of being silently
    dropped; the flat keys are only a fallback for an older payload.

  - A STALE NUMBER ANNOUNCES ITSELF. The endpoint rate-limits, so answers are
    cached briefly, and when a refresh fails the last known reading is returned
    WITH ITS AGE (`obsolete_depuis`) rather than an error or, worse, a silent
    lie. Same reflex as Claude Code's own "Showing last-known usage".

Tool descriptions are in French - runtime UX for the household agents.
"""

import asyncio
import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from ..auth import MACHINE_SUB_PREFIX, current_claims
from ._common import TIMEOUT, enrol_page, new_server, remote_user

logging.getLogger("httpx").setLevel(logging.WARNING)

# Deliberately NOT `identity = "user"`: see the identity paragraph above. The
# hub would then refuse machine tokens on the whole path, enrolment included -
# and enrolment does not need that refusal (the ingress SSO guards it), while
# reading must not have it.

# No `required_env`: nothing to provision. The OAuth client is Claude Code's own
# PUBLIC identifier (it is what the account's own /api/oauth/profile reports as
# `application.uuid`), and the only secret - the refresh token - arrives by
# enrolment and lives on the addon's volume.
mcp = new_server("quotas")

API = "https://api.anthropic.com"
TOKEN_URL = f"{API}/v1/oauth/token"
USAGE_URL = f"{API}/api/oauth/usage"
PROFILE_URL = f"{API}/api/oauth/profile"

CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_BETA = "oauth-2025-04-20"

# See fact 3 in the module docstring: this is not decoration. Overridable so a
# future Claude Code version can be tracked without a release here.
USER_AGENT = os.environ.get("ROSETTA_CLAUDE_UA", "claude-code/2.1.274")

DEFAULT_TZ = "Europe/Paris"

# The endpoint rate-limits; Claude Code itself polls sparingly. Sixty seconds is
# short enough that an agent watching a long run sees movement, long enough that
# a chatty agent cannot get the account throttled.
USAGE_TTL = 60.0
# Past this, a cached reading stops being "the current state" and is reported as
# an explicit staleness instead. Claude Code draws the same line at one hour.
STALE_AFTER = 3600.0

# Test hook: tests inject an httpx.MockTransport here.
_transport = None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, transport=_transport)


# --------------------------------------------------------------------------
# Providers - the extension point
# --------------------------------------------------------------------------

# name -> human label + what its record needs. Adding a provider means adding an
# entry plus its `_fetch_<name>` function; the tools, the store, the enrolment
# page and /health need no change.
PROVIDERS: dict[str, dict] = {
    "claude": {
        "label": "Claude (abonnement claude.ai)",
        "credential": "refresh_token",
        "hint": "jeton de renouvellement d'un login Claude Code",
    },
}
DEFAULT_PROVIDER = "claude"


# --------------------------------------------------------------------------
# Per-user credential store (server-side only)
# --------------------------------------------------------------------------

def _data_dir() -> str:
    return os.environ.get("ROSETTA_QUOTAS_DATA", "/data/quotas")


def _safe(sub: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", sub)[:64]


def _user_file(sub: str) -> str:
    return os.path.join(_data_dir(), "users", f"{_safe(sub)}.json")


def _current_sub() -> str | None:
    claims = current_claims.get()
    if not claims:
        return None
    # Same key as google and withings: Authelia access tokens may carry an
    # opaque `sub`, so the username claim wins, NFC-normalized to match what
    # enrolment filed it under.
    value = claims.get("preferred_username") or claims.get("sub")
    return unicodedata.normalize("NFC", str(value)) if value else None


def _is_machine() -> bool:
    claims = current_claims.get() or {}
    return str(claims.get("sub", "")).startswith(MACHINE_SUB_PREFIX)


def _machine_allowed() -> bool:
    """`ROSETTA_QUOTAS_CLIENTS` = comma-separated machine subjects. Empty (the
    default) admits every machine identity the hub already authenticated - the
    narrowing exists for a deployment that wants it, not as a pretence that an
    allowlist of subjects is a second factor."""
    allowed = os.environ.get("ROSETTA_QUOTAS_CLIENTS", "").strip()
    if not allowed:
        return True
    claims = current_claims.get() or {}
    sub = str(claims.get("sub", ""))
    bare = sub[len(MACHINE_SUB_PREFIX):] if sub.startswith(MACHINE_SUB_PREFIX) else sub
    return bare in {c.strip() for c in allowed.split(",") if c.strip()}


def _enrolled_subjects() -> list[str]:
    try:
        names = os.listdir(os.path.join(_data_dir(), "users"))
    except OSError:
        return []
    return sorted(n[:-5] for n in names if n.endswith(".json"))


def _target_sub() -> str | dict:
    """Whose gauges this call reads, or an {'error': ...} dict.

    A human reads their own. A machine reads the deployment's designated owner -
    explicitly configured, or inferred only when the inference cannot be wrong.
    """
    if not _is_machine():
        sub = _current_sub()
        return sub or {"error": "identité absente du contexte d'appel."}

    if not _machine_allowed():
        return {"error": "cette identité machine n'est pas autorisée à lire les "
                         "compteurs (ROSETTA_QUOTAS_CLIENTS)."}

    owner = (os.environ.get("ROSETTA_QUOTAS_OWNER") or "").strip()
    if owner:
        return unicodedata.normalize("NFC", owner)

    enrolled = _enrolled_subjects()
    if len(enrolled) == 1:
        return enrolled[0]
    if not enrolled:
        external = os.environ.get("ROSETTA_EXTERNAL_URL", "").rstrip("/")
        return {"error": "aucun abonnement enrôlé sur ce hub : ouvrir "
                         f"{external}/quotas/enroll dans un navigateur."}
    return {"error": "plusieurs abonnements sont enrôlés et aucun propriétaire n'est "
                     "désigné : un appel machine ne peut pas deviner lesquels lire. "
                     "Renseigner ROSETTA_QUOTAS_OWNER."}


def _read_user(sub: str) -> dict:
    try:
        with open(_user_file(sub)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_user(sub: str, record: dict) -> None:
    """Atomic: the refresh token may rotate, and a half-written file is a lost
    credential - there is no way to re-derive it but another browser login."""
    os.makedirs(os.path.join(_data_dir(), "users"), exist_ok=True)
    path = _user_file(sub)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(record, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _enrol_hint(sub: str, provider: str) -> str:
    external = os.environ.get("ROSETTA_EXTERNAL_URL", "").rstrip("/")
    return (f"aucun abonnement « {provider} » enrôlé pour « {sub} ». Ouvrir "
            f"{external}/quotas/enroll dans un navigateur pour le déclarer "
            "(une seule fois).")


def _key(sub: str, provider: str) -> tuple[str, str]:
    """The cache and lock key for a subject, derived through `_safe()` - the very
    function that derives the FILE.

    ⚠️ Not a detail, and not cosmetic. One subject reaches this module under two
    spellings: a human call carries the Authelia username (`Sébastien`), a
    machine call carries the enrolled FILENAME, which `_safe()` has already
    folded (`S_bastien`). Both resolve to the same file, because `_safe()` is
    idempotent - so keying the lock on the raw spelling gives the same
    credential TWO different locks, and the serialization that exists to stop a
    rotated refresh token from being burned twice stops serializing exactly
    where the two kinds of caller meet. Everything that keys on a subject goes
    through here.
    """
    return (_safe(sub), provider)


# Access-token cache: (safe sub, provider) -> (token, epoch expiry). Not a mere
# optimization: a refresh may rotate the stored credential, so refreshing per
# call would multiply the windows in which a crash loses it.
_token_cache: dict[tuple[str, str], tuple[str, float]] = {}
# Last successful reading: (sub, provider) -> (payload, epoch). This is what
# makes a rate-limited answer honest instead of empty.
_usage_cache: dict[tuple[str, str], tuple[dict, float]] = {}
# One refresh at a time per user and provider, for the rotation reason above.
_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _lock(key: tuple[str, str]) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


# --------------------------------------------------------------------------
# Claude: credential refresh
# --------------------------------------------------------------------------

async def _claude_access_token(sub: str, force: bool = False) -> str | dict:
    """A live Claude access token for `sub`, or an {'error': ...} dict."""
    key = _key(sub, "claude")
    if not force:
        cached = _token_cache.get(key)
        if cached and time.time() < cached[1]:
            return cached[0]

    async with _lock(key):
        cached = _token_cache.get(key)
        if cached and time.time() < cached[1] and not force:
            return cached[0]

        user = _read_user(sub)
        record = (user.get("providers") or {}).get("claude") or {}
        if not record.get("refresh_token"):
            return {"error": _enrol_hint(sub, "claude")}

        async with _client() as http:
            try:
                r = await http.post(TOKEN_URL, json={
                    "grant_type": "refresh_token",
                    "refresh_token": record["refresh_token"],
                    "client_id": CLAUDE_CLIENT_ID,
                })
            except httpx.HTTPError as exc:
                return {"error": f"api.anthropic.com injoignable ({type(exc).__name__})."}
            try:
                data = r.json()
            except ValueError:
                return {"error": f"réponse illisible du jeton Claude (HTTP {r.status_code})."}

        if r.status_code >= 400 or not data.get("access_token"):
            if data.get("error") == "invalid_grant":
                return {"error": f"l'autorisation Claude de « {sub} » a été révoquée ou a "
                                 "expiré : ré-enrôlement nécessaire (/quotas/enroll)."}
            return {"error": "rafraîchissement du jeton Claude impossible "
                             f"({data.get('error') or f'HTTP {r.status_code}'})."}

        # Persist a ROTATED credential before handing the access token out: if
        # the service rotates, the one just used is already dead upstream.
        if data.get("refresh_token") and data["refresh_token"] != record["refresh_token"]:
            record["refresh_token"] = data["refresh_token"]
            record["refreshed_at"] = int(time.time())
            user.setdefault("providers", {})["claude"] = record
            _write_user(sub, user)

        token = data["access_token"]
        _token_cache[key] = (token, time.time() + int(data.get("expires_in", 3600)) - 120)
        return token


def _claude_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA,
        # Fact 3: omit this and a valid token still gets 429 forever.
        "User-Agent": USER_AGENT,
    }


async def _fetch_claude(sub: str) -> dict:
    """The raw usage payload for `sub`, or {'error': ..., 'status': int?}."""
    token = await _claude_access_token(sub)
    if isinstance(token, dict):
        return token

    for attempt in (1, 2):
        async with _client() as http:
            try:
                r = await http.get(USAGE_URL, headers=_claude_headers(token))
            except httpx.HTTPError as exc:
                return {"error": f"api.anthropic.com injoignable ({type(exc).__name__})."}
        if r.status_code == 401 and attempt == 1:
            # The cached access token was revoked before its stated expiry.
            token = await _claude_access_token(sub, force=True)
            if isinstance(token, dict):
                return token
            continue
        if r.status_code == 403:
            return {"error": "ce credential n'a pas la portée « user:profile » exigée par "
                             "l'endpoint. Un jeton issu de `claude setup-token` ne l'a "
                             "jamais : il faut celui d'un vrai login (/quotas/enroll).",
                    "status": 403}
        if r.status_code == 429:
            return {"error": "Anthropic limite les appels à ses compteurs.", "status": 429}
        if r.status_code >= 400:
            return {"error": f"l'endpoint des compteurs a répondu HTTP {r.status_code}.",
                    "status": r.status_code}
        try:
            return r.json()
        except ValueError:
            return {"error": "réponse illisible de l'endpoint des compteurs."}
    return {"error": "l'endpoint des compteurs refuse ce credential."}


# --------------------------------------------------------------------------
# Shaping
# --------------------------------------------------------------------------

def _tz():
    try:
        return ZoneInfo(os.environ.get("TZ") or DEFAULT_TZ)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def _local(iso: str | None) -> str | None:
    """An upstream UTC timestamp, rendered where the household lives."""
    if not iso:
        return None
    try:
        moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_tz()).strftime("%Y-%m-%d %H:%M")


def _in(iso: str | None) -> str | None:
    """« dans 1 h 20 » - the number one actually acts on."""
    if not iso:
        return None
    try:
        moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    delta = int((moment - datetime.now(timezone.utc)).total_seconds())
    if delta <= 0:
        return "maintenant"
    days, rest = divmod(delta, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"dans {days} j {hours} h"
    if hours:
        return f"dans {hours} h {minutes:02d}"
    return f"dans {minutes} min"


def _compact(mapping: dict) -> dict:
    return {k: v for k, v in mapping.items() if v is not None}


# `kind` values seen in the wild -> what they mean to a human. An unknown kind
# is NOT dropped: it is passed through under its raw name, because the whole
# point of reading `limits[]` is to survive a bucket Anthropic adds later.
KINDS = {
    "session": "fenêtre de 5 h",
    "weekly_all": "semaine (tous modèles)",
    "weekly_scoped": "semaine (modèle dédié)",
}


def _limit_row(entry: dict) -> dict:
    scope = entry.get("scope") or {}
    model = (scope.get("model") or {}).get("display_name")
    resets = entry.get("resets_at")
    return _compact({
        "compteur": KINDS.get(entry.get("kind"), entry.get("kind")),
        "modele": model,
        "surface": scope.get("surface"),
        "pourcent": entry.get("percent"),
        "gravite": entry.get("severity") if entry.get("severity") != "normal" else None,
        "en_cours": entry.get("is_active") or None,
        "remise_a_zero": _local(resets),
        "dans": _in(resets),
    })


# Legacy flat keys, used ONLY when `limits[]` is absent (older payload). The
# dozen codename buckets are deliberately NOT listed: they are null on a normal
# account, and enumerating unreleased feature names here would age badly.
LEGACY = {"five_hour": "fenêtre de 5 h", "seven_day": "semaine (tous modèles)",
          "seven_day_opus": "semaine (Opus)", "seven_day_sonnet": "semaine (Sonnet)"}


def _legacy_rows(payload: dict) -> list[dict]:
    rows = []
    for key, label in LEGACY.items():
        bucket = payload.get(key)
        if not isinstance(bucket, dict):
            continue
        rows.append(_compact({
            "compteur": label,
            "pourcent": bucket.get("utilization"),
            "remise_a_zero": _local(bucket.get("resets_at")),
            "dans": _in(bucket.get("resets_at")),
        }))
    return rows


def _money(amount: dict | None) -> str | None:
    """{'amount_minor': 309, 'currency': 'EUR', 'exponent': 2} -> '3.09 EUR'."""
    if not isinstance(amount, dict) or amount.get("amount_minor") is None:
        return None
    exponent = amount.get("exponent") or 0
    value = amount["amount_minor"] / (10 ** exponent)
    return f"{value:.{exponent}f} {amount.get('currency') or ''}".strip()


def _credits(payload: dict) -> dict | None:
    """The paid-overflow state - the safety net for when a plan limit is hit.

    Worth its own block rather than a line: whether it is ON is what decides if
    hitting the plan limit means "slow down" or "stop working".
    """
    spend = payload.get("spend")
    extra = payload.get("extra_usage") or {}
    if not isinstance(spend, dict) and not extra:
        return None
    spend = spend if isinstance(spend, dict) else {}
    enabled = spend.get("enabled", extra.get("is_enabled"))
    return _compact({
        "actif": enabled,
        "depense": _money(spend.get("used")),
        "plafond": _money(spend.get("limit")),
        "pourcent": spend.get("percent", extra.get("utilization")),
        "gravite": spend.get("severity") if spend.get("severity") != "normal" else None,
        "motif_desactivation": spend.get("disabled_reason") or extra.get("disabled_reason"),
        "plafond_atteint": extra.get("spend_limit_reached") or None,
        "peut_activer": spend.get("can_toggle"),
    })


def _breakdown(payload: dict) -> dict | None:
    """Where the week actually went, by surface. Zero rows are dropped: an agent
    reading "Cowork 0 %" learns nothing it could act on."""
    block = payload.get("seven_day_breakdown")
    if not isinstance(block, dict):
        return None
    rows = {row.get("display_name") or row.get("key"): row.get("percent")
            for row in (block.get("rows") or []) if row.get("percent")}
    if not rows:
        return None
    return {"depuis": _local(block.get("window_started_at")), "repartition": rows}


def _shape_claude(payload: dict) -> dict:
    limits = payload.get("limits")
    if isinstance(limits, list) and limits:
        rows = [_limit_row(e) for e in limits if isinstance(e, dict)]
        source = "limits[]"
    else:
        # Nothing matched the modern shape. Fall back, and SAY that we did -
        # a silent fallback is how a payload change becomes a wrong answer.
        rows = _legacy_rows(payload)
        source = "clés historiques (le tableau `limits` a disparu du format amont)"
    answer = _compact({
        "fournisseur": "claude",
        "compteurs": rows or None,
        "credits_supplementaires": _credits(payload),
        "semaine_par_surface": _breakdown(payload),
        "lu_dans": source,
    })
    if not rows:
        answer["avertissement"] = (
            "aucun compteur reconnu dans la réponse d'Anthropic : le format amont a "
            "changé. Les chiffres ne sont PAS à jour, ils sont absents.")
    return answer


FETCHERS = {"claude": _fetch_claude}
SHAPERS = {"claude": _shape_claude}


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@mcp.tool()
async def quotas(fournisseur: str = DEFAULT_PROVIDER) -> dict:
    """Où en sont les compteurs d'usage de l'abonnement : ce qui est consommé, et quand ça se remet à zéro.

    Répond à « est-ce que je peux encore lancer un gros truc ? » et « ça repart
    quand ? ». Rend chaque compteur avec son pourcentage, l'heure de sa remise à
    zéro et le délai d'ici là — plus l'état des crédits supplémentaires (le filet
    quand le plafond du forfait est atteint) et la répartition de la semaine par
    surface.

    ⚠️ Ces chiffres viennent d'un endpoint **non documenté** d'Anthropic, celui
    qu'utilise Claude Code lui-même. Il peut changer sans préavis : le jour où il
    change, cet outil le dit au lieu d'inventer. Si la lecture du moment échoue,
    la dernière connue est rendue avec son âge (`obsolete_depuis`) — jamais un
    chiffre périmé présenté comme frais.

    Args:
        fournisseur: le service à interroger. Par défaut « claude ».
    """
    fournisseur = (fournisseur or DEFAULT_PROVIDER).strip().lower()
    if fournisseur not in PROVIDERS:
        connus = ", ".join(sorted(PROVIDERS))
        return {"error": f"fournisseur « {fournisseur} » inconnu. Connus : {connus}."}

    sub = _target_sub()
    if isinstance(sub, dict):
        return sub

    key = _key(sub, fournisseur)
    cached = _usage_cache.get(key)
    if cached and time.time() - cached[1] < USAGE_TTL:
        return SHAPERS[fournisseur](cached[0])

    payload = await FETCHERS[fournisseur](sub)
    if "error" in payload:
        # A failed read does not erase what we already knew - it dates it.
        if cached and time.time() - cached[1] < STALE_AFTER:
            stale = SHAPERS[fournisseur](cached[0])
            age = int(time.time() - cached[1])
            stale["obsolete_depuis"] = f"{age // 60} min" if age >= 60 else f"{age} s"
            stale["avertissement"] = (
                f"lecture du moment impossible ({payload['error']}) — chiffres datés.")
            return stale
        return {k: v for k, v in payload.items() if k != "status"}

    _usage_cache[key] = (payload, time.time())
    return SHAPERS[fournisseur](payload)


@mcp.tool()
async def quotas_fournisseurs() -> dict:
    """Quels abonnements cet addon sait lire, et lesquels sont déjà enrôlés pour l'appelant.

    Utile avant de conclure « je n'ai pas l'info » : un fournisseur listé mais
    non enrôlé se règle en ouvrant la page d'enrôlement, une fois.
    """
    sub = _target_sub()
    if isinstance(sub, dict):
        return sub
    user = _read_user(sub)
    enrolled = user.get("providers") or {}
    external = os.environ.get("ROSETTA_EXTERNAL_URL", "").rstrip("/")
    return {
        "utilisateur": user.get("sub") or sub,
        "fournisseurs": [
            _compact({
                "nom": name,
                "libelle": meta["label"],
                "enrole": name in enrolled,
                "enrole_le": _local(datetime.fromtimestamp(
                    enrolled[name]["enrolled_at"], timezone.utc).isoformat())
                if enrolled.get(name, {}).get("enrolled_at") else None,
            })
            for name, meta in sorted(PROVIDERS.items())
        ],
        "enrolement": f"{external}/quotas/enroll" if external else "/quotas/enroll",
    }


# --------------------------------------------------------------------------
# Enrolment (browser form, guarded by the ingress forwardAuth)
# --------------------------------------------------------------------------

# Why a pasted credential rather than an OAuth redirect like google/withings:
# the only client able to mint a `user:profile` credential for a claude.ai
# subscription is Claude Code itself, and its authorization flow redirects to a
# callback the hub does not own. Asking for the result of a real login is honest
# about that; inventing a consent screen for a client we are not would not be.
FORM = """
<form method="post" style="margin-top:1.2rem;text-align:left">
  <label style="font-size:.8rem;opacity:.7">Jeton de renouvellement</label>
  <textarea name="refresh_token" rows="3" required
    style="width:100%;margin:.4rem 0 .9rem;padding:.6rem;border-radius:8px;
           border:1px solid rgba(128,128,128,.4);background:transparent;
           color:inherit;font-family:ui-monospace,monospace;font-size:.8rem"
    placeholder="sk-ant-ort01-..."></textarea>
  <button type="submit" style="width:100%;padding:.7rem;border:0;border-radius:8px;
    background:#2b2b2b;color:#fff;font-size:.9rem;cursor:pointer">Enrôler</button>
</form>
<details style="margin-top:1.4rem;text-align:left;font-size:.82rem;opacity:.85">
  <summary style="cursor:pointer">Comment obtenir ce jeton</summary>
  <p style="line-height:1.5">Il faut celui d'un <b>vrai login</b> : un jeton issu de
  <code>claude setup-token</code> est refusé par l'endpoint des compteurs (portée
  <code>user:profile</code> absente). Faites le login dans un dossier jetable, pour
  ne pas toucher à votre session habituelle :</p>
  <pre style="overflow-x:auto;padding:.7rem;border-radius:8px;
    background:rgba(128,128,128,.12);font-size:.74rem">docker run --rm -it -v "$PWD/cred:/cred" \\
  -e CLAUDE_CONFIG_DIR=/cred node:22 \\
  npx -y @anthropic-ai/claude-code</pre>
  <p style="line-height:1.5">Connectez-vous, sortez, puis relevez
  <code>refresh_token</code> dans <code>cred/.credentials.json</code> (clé
  <code>claudeAiOauth</code>). Effacez le dossier ensuite : le jeton vit
  désormais ici.</p>
</details>
"""


async def enroll(request):
    """GET: the form. POST: probe the pasted credential, then keep it."""
    sub = remote_user(request)
    if not sub:
        return enrol_page("quotas", "🔒", "Identité absente",
                          "Cette page doit être ouverte à travers le portail "
                          "d'authentification.", status=403)

    if request.method == "GET":
        return enrol_page(
            "quotas", "📊", f"Enrôler un abonnement pour « {sub} »",
            "Collez le jeton de renouvellement d'un login Claude. Il reste sur le "
            "serveur : les agents ne le voient jamais.", extra=FORM)

    form = await request.form()
    token = (form.get("refresh_token") or "").strip()
    if not token:
        return enrol_page("quotas", "⚠️", "Jeton manquant",
                          "Aucun jeton n'a été fourni.", extra=FORM, status=400)

    # Probe BEFORE storing. A credential that cannot read the counters is worse
    # than no credential: it turns every later call into a puzzle. This is also
    # where a `setup-token` is caught, with the reason spelled out.
    user = _read_user(sub)
    # The real spelling, kept because the filename has lost its accents and an
    # answer that calls the user « S_bastien » is an answer that read a path.
    user["sub"] = sub
    user.setdefault("providers", {})["claude"] = {
        "refresh_token": token, "enrolled_at": int(time.time()),
    }
    _token_cache.pop(_key(sub, "claude"), None)
    previous = _read_user(sub)
    _write_user(sub, user)

    probe = await _fetch_claude(sub)
    if "error" in probe:
        # Put back exactly what was there, including "nothing".
        _write_user(sub, previous)
        _token_cache.pop(_key(sub, "claude"), None)
        return enrol_page("quotas", "⚠️", "Jeton refusé", probe["error"],
                          extra=FORM, status=400)

    _usage_cache[_key(sub, "claude")] = (probe, time.time())
    rows = _shape_claude(probe).get("compteurs") or []
    summary = ", ".join(f"{r.get('compteur')} {r.get('pourcent')} %" for r in rows[:3])
    return enrol_page("quotas", "✅", "Abonnement Claude enrôlé",
                      f"Lecture vérifiée à l'instant : {summary or 'compteurs accessibles'}.")


extra_routes = [("/enroll", enroll, ["GET", "POST"])]
open_paths = ["/enroll"]
