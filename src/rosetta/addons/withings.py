"""`withings` addon - Health Mate body metrics, user-data class, READ-ONLY.

Contract (the guard IS the tool surface: nothing here writes anything):
  - withings_measures : weight, body composition, blood pressure, ECG intervals -
    every measure type the scales, watches and monitors ever recorded
  - withings_activity : daily aggregates (steps, distance, calories, heart rate)
  - withings_sleep    : nightly summaries (phases, score, respiration, snoring)
  - withings_workouts : logged sessions
  - withings_devices  : the hardware itself, with battery level

Identity: `identity = "user"` - the hub refuses machine tokens on /withings, so
every call carries a human `sub` (Authelia). Withings credentials are stored
SERVER-SIDE, one file per subject under ROSETTA_WITHINGS_DATA; agents never see
them. Enrolment is a one-time browser flow (/withings/enroll -> Withings consent
-> /withings/callback), guarded by the ingress forwardAuth (Remote-User header).

Three Withings quirks shape this module. All three fail silently if ignored:

  1. Every call is a form POST, and every answer is HTTP 200 - including the
     failures. The real outcome is `status` INSIDE the JSON body (0 = success).
     Trusting the HTTP code means reading an expired token as valid data.
  2. The refresh token ROTATES: each refresh mints a new one and kills the old.
     It must be persisted BEFORE the access token is used, and refreshes must be
     serialized per user - two concurrent refreshes lose the account, the second
     presenting a token the first already burned. Hence: ONE replica, and the
     access token is cached in memory for its full lifetime.
  3. A measure is a (value, unit) pair where `unit` is a POWER OF TEN, not a
     physical unit: real = value * 10^unit. Read it as kilograms and a weight of
     78.192 kg arrives as 78192.

Tool descriptions are in French - runtime UX for the household agents.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from starlette.responses import RedirectResponse

from ..auth import current_claims
from ._common import TIMEOUT, enrol_page, new_server

logging.getLogger("httpx").setLevel(logging.WARNING)

identity = "user"

# Provisioned as environment (unlike google's client_secret.json, which is a file
# Google hands out): a missing key degrades the addon on /health instead of
# failing at the first call.
required_env = ["WITHINGS_CLIENT_ID", "WITHINGS_CLIENT_SECRET"]

mcp = new_server("withings")

API = "https://wbsapi.withings.net"
TOKEN_URL = f"{API}/v2/oauth2"
AUTH_URL = "https://account.withings.com/oauth2_user/authorize2"

# Comma-separated, not space-separated - Withings departs from OAuth 2.1 here too.
SCOPES = ["user.info", "user.metrics", "user.activity", "user.sleepevents"]

DEFAULT_TZ = "Europe/Paris"

# `status` values in the JSON body. 0 is success; the rest is a long enumeration
# of which only the authentication family needs distinct handling (it is the one
# an agent can act on: re-enrol).
STATUS_OK = 0
STATUS_AUTH_FAILED = {100, 101, 102, 200, 401}
STATUS_TOO_MANY_REQUESTS = 601

# Test hook: tests inject an httpx.MockTransport here.
_transport = None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, transport=_transport)


# --------------------------------------------------------------------------
# Vocabulary - measure types, positions, workout categories
# --------------------------------------------------------------------------

# code -> (French label, unit). The label doubles as the filter vocabulary of
# `withings_measures`. A `None` unit means Withings publishes a bare index or
# score (or does not document one) - the value still comes back untouched, which
# is why guessing a unit here would be worse than leaving it out.
MEASURE_TYPES: dict[int, tuple[str, str | None]] = {
    1: ("poids", "kg"),
    4: ("taille", "m"),
    5: ("masse maigre", "kg"),
    6: ("taux de masse grasse", "%"),
    8: ("masse grasse", "kg"),
    9: ("tension diastolique", "mmHg"),
    10: ("tension systolique", "mmHg"),
    11: ("pouls", "bpm"),
    12: ("température", "°C"),
    54: ("SpO2", "%"),
    71: ("température corporelle", "°C"),
    73: ("température cutanée", "°C"),
    76: ("masse musculaire", "kg"),
    77: ("hydratation", "kg"),
    88: ("masse osseuse", "kg"),
    91: ("vitesse de l'onde de pouls", "m/s"),
    123: ("VO2 max", "ml/min/kg"),
    130: ("fibrillation auriculaire (ECG)", None),
    135: ("intervalle QRS", "ms"),
    136: ("intervalle PR", "ms"),
    137: ("intervalle QT", "ms"),
    138: ("intervalle QT corrigé", "ms"),
    139: ("fibrillation auriculaire (PPG)", None),
    155: ("âge vasculaire", None),
    158: ("santé nerveuse (pied gauche)", None),
    159: ("santé nerveuse (pied droit)", None),
    167: ("santé nerveuse (deux pieds)", None),
    168: ("eau extracellulaire", "kg"),
    169: ("eau intracellulaire", "kg"),
    170: ("graisse viscérale", None),
    173: ("masse maigre segmentaire", "kg"),
    174: ("masse grasse segmentaire", "kg"),
    175: ("masse musculaire segmentaire", "kg"),
    196: ("activité électrodermale (pieds)", "%"),
    197: ("activité électrodermale (pied gauche)", "%"),
    198: ("activité électrodermale (pied droit)", "%"),
    226: ("métabolisme de base", None),
    227: ("âge métabolique", None),
}

# Shorthands an agent (or a human) is likely to type, on top of the labels above.
MEASURE_ALIASES: dict[str, tuple[int, ...]] = {
    "weight": (1,),
    "height": (4,),
    "imc": (1, 4),
    "masse grasse": (8, 6),
    "graisse": (8, 6),
    "fat": (8, 6),
    "tension": (10, 9),
    "pression arterielle": (10, 9),
    "blood pressure": (10, 9),
    "coeur": (11,),
    "heart rate": (11,),
    "pulse": (11,),
    "composition corporelle": (1, 6, 8, 5, 76, 88, 77),
    "ecg": (130, 135, 136, 137, 138, 139),
}

# Where a segmental measure was taken (types 173/174/175 repeat per body part).
MEASURE_POSITIONS: dict[int, str] = {
    0: "poignet droit", 1: "poignet gauche", 2: "bras droit", 3: "bras gauche",
    4: "pied droit", 5: "pied gauche", 6: "entre les jambes", 7: "corps entier",
    8: "côté gauche", 9: "côté droit", 10: "jambe gauche", 11: "jambe droite",
    12: "torse", 13: "main gauche", 14: "main droite",
}

# attrib values that mean "typed in by hand" rather than "measured by a device" -
# worth surfacing: a manual entry is not evidence of anything.
MANUAL_ATTRIB = {2, 4}

WORKOUT_CATEGORIES: dict[int, str] = {
    1: "marche", 2: "course", 3: "randonnée", 4: "roller", 5: "BMX",
    6: "vélo", 7: "natation", 8: "surf", 9: "kitesurf", 10: "planche à voile",
    11: "bodyboard", 12: "tennis", 13: "tennis de table", 14: "squash",
    15: "badminton", 16: "musculation", 17: "gymnastique", 18: "elliptique",
    19: "pilates", 20: "basket", 21: "football", 22: "football américain",
    23: "rugby", 24: "volley", 25: "water-polo", 26: "équitation", 27: "golf",
    28: "yoga", 29: "danse", 30: "boxe", 31: "escrime", 32: "lutte",
    33: "arts martiaux", 34: "ski", 35: "snowboard", 36: "autre",
    128: "aucune activité", 187: "aviron", 188: "zumba", 191: "baseball",
    192: "handball", 193: "hockey", 194: "hockey sur glace", 195: "escalade",
    196: "patinage", 272: "multi-sport", 306: "marche en intérieur",
    307: "course en intérieur", 308: "vélo en intérieur",
}

ACTIVITY_FIELDS = [
    "steps", "distance", "elevation", "soft", "moderate", "intense", "active",
    "calories", "totalcalories", "hr_average", "hr_min", "hr_max",
    "hr_zone_0", "hr_zone_1", "hr_zone_2", "hr_zone_3",
]

SLEEP_FIELDS = [
    "total_sleep_time", "total_timeinbed", "sleep_efficiency", "sleep_latency",
    "wakeup_latency", "waso", "nb_rem_episodes", "sleep_score",
    "deepsleepduration", "lightsleepduration", "remsleepduration",
    "wakeupduration", "wakeupcount", "out_of_bed_count",
    "hr_average", "hr_min", "hr_max", "rr_average", "rr_min", "rr_max",
    "snoring", "snoringepisodecount", "apnea_hypopnea_index",
    "breathing_disturbances_intensity",
]

WORKOUT_FIELDS = [
    "calories", "intensity", "distance", "elevation", "steps",
    "hr_average", "hr_min", "hr_max", "spo2_average", "pause_duration",
    "pool_laps", "strokes", "pool_length",
]


# --------------------------------------------------------------------------
# Per-user credential store (server-side only)
# --------------------------------------------------------------------------

def _data_dir() -> str:
    return os.environ.get("ROSETTA_WITHINGS_DATA", "/data/withings")


def _safe(sub: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", sub)[:64]


def _user_file(sub: str) -> str:
    return os.path.join(_data_dir(), "users", f"{_safe(sub)}.json")


def _oauth_client() -> dict | str:
    client_id = os.environ.get("WITHINGS_CLIENT_ID")
    client_secret = os.environ.get("WITHINGS_CLIENT_SECRET")
    if not client_id or not client_secret:
        return ("configuration Withings absente (WITHINGS_CLIENT_ID / "
                "WITHINGS_CLIENT_SECRET) : l'addon n'est pas provisionné.")
    return {"client_id": client_id, "client_secret": client_secret}


def _current_sub() -> str | None:
    claims = current_claims.get()
    if not claims:
        return None
    # Same key as the google addon: Authelia access tokens may carry an opaque
    # `sub`, so the username claim wins, NFC-normalized to match enrolment.
    value = claims.get("preferred_username") or claims.get("sub")
    return unicodedata.normalize("NFC", str(value)) if value else None


def _read_user(sub: str) -> dict | None:
    try:
        with open(_user_file(sub)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_user(sub: str, record: dict) -> None:
    """Atomic: the refresh token rotates, and a half-written file is a lost
    account (no way to re-derive it but a browser re-enrolment)."""
    os.makedirs(os.path.join(_data_dir(), "users"), exist_ok=True)
    path = _user_file(sub)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(record, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _enrol_hint(sub: str) -> str:
    external = os.environ.get("ROSETTA_EXTERNAL_URL", "")
    return (f"aucun compte Withings enrôlé pour « {sub} ». Ouvrir "
            f"{external.rstrip('/')}/withings/enroll dans un navigateur pour "
            "autoriser l'accès (une seule fois).")


# Access-token cache: sub -> (token, epoch expiry). Not a mere optimization -
# every refresh burns the stored refresh token, so refreshing per call would
# multiply the windows in which a crash loses the account.
_token_cache: dict[str, tuple[str, float]] = {}
# One refresh at a time per user, for the same reason.
_refresh_locks: dict[str, asyncio.Lock] = {}


def _lock(sub: str) -> asyncio.Lock:
    lock = _refresh_locks.get(sub)
    if lock is None:
        lock = _refresh_locks[sub] = asyncio.Lock()
    return lock


async def _access_token(sub: str, force: bool = False) -> str | dict:
    """A live Withings access token for `sub`, or an {'error': ...} dict."""
    if not force:
        cached = _token_cache.get(sub)
        if cached and time.time() < cached[1]:
            return cached[0]

    client = _oauth_client()
    if isinstance(client, str):
        return {"error": client}

    async with _lock(sub):
        # Re-check inside the lock: a concurrent caller may have just refreshed,
        # and its token is the only valid one now.
        cached = _token_cache.get(sub)
        if cached and time.time() < cached[1] and not force:
            return cached[0]
        user = _read_user(sub)
        if not user or not user.get("refresh_token"):
            return {"error": _enrol_hint(sub)}
        async with _client() as http:
            r = await http.post(TOKEN_URL, data={
                "action": "requesttoken",
                "grant_type": "refresh_token",
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "refresh_token": user["refresh_token"],
            })
            try:
                data = r.json()
            except ValueError:
                return {"error": f"réponse illisible du jeton Withings (HTTP {r.status_code})."}
        status = data.get("status")
        body = data.get("body") or {}
        if status != STATUS_OK or not body.get("access_token"):
            if status in STATUS_AUTH_FAILED:
                return {"error": f"l'autorisation Withings de « {sub} » a été révoquée ou a "
                                 "expiré : ré-enrôlement nécessaire (/withings/enroll)."}
            return {"error": f"rafraîchissement du jeton Withings impossible (status {status})."}

        # Persist the ROTATED refresh token before handing the access token out:
        # the one we just used is already dead on Withings' side.
        if body.get("refresh_token"):
            user["refresh_token"] = body["refresh_token"]
            user["refreshed_at"] = int(time.time())
            if body.get("userid"):
                user["userid"] = body["userid"]
            _write_user(sub, user)

        token = body["access_token"]
        _token_cache[sub] = (token, time.time() + int(body.get("expires_in", 10800)) - 60)
        return token


async def _call(path: str, data: dict) -> dict:
    """POST a Withings action for the calling user; returns its `body`, or
    {'error': ...}. Retries once on an auth failure, in case the cached access
    token was revoked before its stated expiry."""
    sub = _current_sub()
    if not sub:
        return {"error": "identité utilisateur absente du contexte d'appel (token machine ?)."}

    for attempt in (0, 1):
        token = await _access_token(sub, force=bool(attempt))
        if isinstance(token, dict):
            return token
        async with _client() as http:
            r = await http.post(f"{API}/{path}", data=data,
                                headers={"Authorization": f"Bearer {token}"})
            try:
                payload = r.json()
            except ValueError:
                return {"error": f"réponse illisible de Withings (HTTP {r.status_code})."}
        status = payload.get("status")
        if status == STATUS_OK:
            return {"body": payload.get("body") or {}}
        if status in STATUS_AUTH_FAILED and attempt == 0:
            _token_cache.pop(sub, None)
            continue
        if status == STATUS_TOO_MANY_REQUESTS:
            return {"error": "quota Withings dépassé (status 601) : réessayer plus tard."}
        detail = payload.get("error") or f"status {status}"
        return {"error": f"Withings a refusé l'appel : {detail}."}
    return {"error": "authentification Withings impossible après rafraîchissement."}


# --------------------------------------------------------------------------
# Dates and values
# --------------------------------------------------------------------------

def _tz() -> tzinfo:
    """The house zone. Falls all the way back to UTC: a bogus TZ (or an image
    without a tz database) must skew the hours, never 500 the tool."""
    for name in (os.environ.get("TZ"), DEFAULT_TZ):
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except Exception:
            continue
    return timezone.utc


def _moment(value: str, end_of_day: bool) -> datetime:
    """ISO date (YYYY-MM-DD) or datetime -> aware datetime in the local zone."""
    raw = value.strip()
    if len(raw) == 10:
        day = date.fromisoformat(raw)
        return datetime.combine(
            day,
            datetime.max.time() if end_of_day else datetime.min.time(),
            tzinfo=_tz(),
        )
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_tz())


def _window(start: str | None, end: str | None, default_days: int
            ) -> tuple[datetime, datetime] | str:
    """(start, end) datetimes from optional ISO inputs, or an error message."""
    try:
        stop = _moment(end, end_of_day=True) if end else datetime.now(tz=_tz())
        begin = (_moment(start, end_of_day=False) if start
                 else stop - timedelta(days=default_days))
    except ValueError as exc:
        return f"date illisible ({exc}) : attendu AAAA-MM-JJ ou un ISO 8601 complet."
    if begin > stop:
        return "fenêtre vide : la date de début est postérieure à la date de fin."
    return begin, stop


def _scaled(measure: dict) -> float:
    """Withings sends (value, unit) where unit is a power of ten. Decimal keeps
    78192 * 10^-3 at 78.192 instead of the 78.19200000000001 a float gives."""
    value = Decimal(int(measure["value"])).scaleb(int(measure.get("unit", 0)))
    return float(value)


def _local(epoch, tz_name: str | None = None) -> str | None:
    """Epoch seconds -> ISO 8601 in the measure's own timezone when Withings
    gives one (a weight taken abroad keeps the hour it was taken at)."""
    if epoch is None:
        return None
    zone = _tz()
    if tz_name:
        try:
            zone = ZoneInfo(tz_name)
        except Exception:
            pass
    return datetime.fromtimestamp(int(epoch), tz=zone).isoformat()


def _norm(text: str) -> str:
    """Fold case, accents and punctuation, so « masse grasse », « Masse Grasse »
    and « masse_grasse » all name the same thing."""
    stripped = unicodedata.normalize("NFD", text.lower())
    stripped = "".join(c for c in stripped if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", stripped).strip()


_BY_NAME: dict[str, tuple[int, ...]] = {
    **{_norm(label): (code,) for code, (label, _unit) in MEASURE_TYPES.items()},
    **{_norm(name): codes for name, codes in MEASURE_ALIASES.items()},
}


def _resolve_types(spec: str) -> list[int] | str:
    """« poids, tension » or « 1,10,9 » -> [1, 10, 9], or an error message."""
    codes: list[int] = []
    for chunk in spec.split(","):
        name = chunk.strip()
        if not name:
            continue
        if name.isdigit():
            codes.append(int(name))
            continue
        found = _BY_NAME.get(_norm(name))
        if not found:
            known = ", ".join(sorted(label for label, _u in MEASURE_TYPES.values()))
            return f"type de mesure inconnu : « {name} ». Types connus : {known}."
        codes.extend(found)
    if not codes:
        return "aucun type de mesure exploitable dans la demande."
    return list(dict.fromkeys(codes))  # dedupe, order kept


def _compact(mapping: dict) -> dict:
    """Drop the keys Withings did not answer - a summary full of nulls reads as
    if the night had no data at all."""
    return {k: v for k, v in mapping.items() if v is not None}


def _minutes(seconds) -> float | None:
    return None if seconds is None else round(int(seconds) / 60, 1)


# --------------------------------------------------------------------------
# Tools - lecture seule, aucune écriture possible
# --------------------------------------------------------------------------

@mcp.tool()
async def withings_measures(start: str | None = None, end: str | None = None,
                            types: str | None = None, max_results: int = 30) -> dict:
    """Mesures Withings : poids, composition corporelle, tension, ECG, SpO2…

    Chaque pesée (ou prise de tension) rend un GROUPE de mesures prises ensemble —
    poids, masse grasse, masse musculaire, eau… — daté, avec sa valeur réelle et son
    unité. Le plus récent d'abord : pour « mon poids actuel », prendre le premier.

    start / end : bornes, AAAA-MM-JJ ou ISO 8601 complet. Par défaut, les 30 derniers
                  jours (une pesée par jour au plus, donc c'est peu volumineux).
    types : filtre, noms ou codes séparés par des virgules — « poids »,
            « poids, tension », « composition corporelle », « 1,6,8 ». Sans filtre,
            TOUT ce que les appareils ont enregistré remonte.
    max_results : nombre de groupes rendus (défaut 30, max 200).

    `manual: true` signale une valeur saisie à la main, pas mesurée par un appareil.
    """
    window = _window(start, end, default_days=30)
    if isinstance(window, str):
        return {"error": window}
    begin, stop = window

    data = {
        "action": "getmeas",
        "category": 1,  # 1 = mesures réelles ; 2 = objectifs saisis par l'utilisateur
        "startdate": int(begin.timestamp()),
        "enddate": int(stop.timestamp()),
    }
    if types:
        codes = _resolve_types(types)
        if isinstance(codes, str):
            return {"error": codes}
        data["meastypes"] = ",".join(str(c) for c in codes)

    result = await _call("measure", data)
    if "error" in result:
        return result
    body = result["body"]

    groups = sorted(body.get("measuregrps") or [],
                    key=lambda g: g.get("date") or 0, reverse=True)
    limit = max(1, min(int(max_results), 200))
    out = []
    for group in groups[:limit]:
        measures = []
        for measure in group.get("measures") or []:
            code = int(measure.get("type", 0))
            label, unit = MEASURE_TYPES.get(code, (f"type {code}", None))
            entry = {"type": label, "value": _scaled(measure)}
            if unit:
                entry["unit"] = unit
            position = measure.get("position")
            if position is not None:
                entry["position"] = MEASURE_POSITIONS.get(int(position), f"position {position}")
            measures.append(entry)
        entry = {"date": _local(group.get("date"), group.get("timezone")),
                 "measures": measures}
        if group.get("attrib") in MANUAL_ATTRIB:
            entry["manual"] = True
        if group.get("comment"):
            entry["comment"] = group["comment"]
        out.append(entry)

    answer: dict = {"groups": out}
    if len(groups) > limit:
        answer["note"] = (f"{len(groups)} groupes sur la période, {limit} rendus — "
                          "resserrer la fenêtre ou filtrer par type pour voir le reste.")
    elif body.get("more"):
        answer["note"] = ("Withings signale d'autres mesures au-delà de cette page : "
                          "resserrer la fenêtre pour les atteindre.")
    return answer


@mcp.tool()
async def withings_activity(start: str | None = None, end: str | None = None) -> dict:
    """Activité quotidienne Withings : pas, distance, calories, fréquence cardiaque.

    Une ligne par jour (agrégat de la montre / du tracker). Les durées sont en
    minutes, les distances en mètres.

    start / end : bornes AAAA-MM-JJ. Par défaut, les 7 derniers jours.
    """
    window = _window(start, end, default_days=7)
    if isinstance(window, str):
        return {"error": window}
    begin, stop = window

    result = await _call("v2/measure", {
        "action": "getactivity",
        "startdateymd": begin.date().isoformat(),
        "enddateymd": stop.date().isoformat(),
        "data_fields": ",".join(ACTIVITY_FIELDS),
    })
    if "error" in result:
        return result

    days = []
    for day in result["body"].get("activities") or []:
        days.append(_compact({
            "date": day.get("date"),
            "steps": day.get("steps"),
            "distance_m": round(day["distance"]) if day.get("distance") else None,
            "elevation_m": day.get("elevation"),
            "calories_active": day.get("calories"),
            "calories_total": day.get("totalcalories"),
            "active_min": _minutes(day.get("active")),
            "soft_min": _minutes(day.get("soft")),
            "moderate_min": _minutes(day.get("moderate")),
            "intense_min": _minutes(day.get("intense")),
            "hr_average": day.get("hr_average"),
            "hr_min": day.get("hr_min"),
            "hr_max": day.get("hr_max"),
        }))
    days.sort(key=lambda d: d.get("date") or "", reverse=True)
    return {"days": days}


@mcp.tool()
async def withings_sleep(start: str | None = None, end: str | None = None) -> dict:
    """Sommeil Withings : une synthèse par nuit (phases, score, respiration, ronflement).

    Durées en minutes. `score` est l'indice Withings sur 100. `apnea_hypopnea_index`
    n'existe qu'avec un appareil qui le mesure ; les champs absents sont omis plutôt
    que rendus à zéro.

    start / end : bornes AAAA-MM-JJ. Par défaut, les 7 dernières nuits.
    La nuit est datée du jour du RÉVEIL (convention Withings).
    """
    window = _window(start, end, default_days=7)
    if isinstance(window, str):
        return {"error": window}
    begin, stop = window

    result = await _call("v2/sleep", {
        "action": "getsummary",
        "startdateymd": begin.date().isoformat(),
        "enddateymd": stop.date().isoformat(),
        "data_fields": ",".join(SLEEP_FIELDS),
    })
    if "error" in result:
        return result

    nights = []
    for night in result["body"].get("series") or []:
        d = night.get("data") or {}
        nights.append(_compact({
            "date": night.get("date"),
            "start": _local(night.get("startdate"), night.get("timezone")),
            "end": _local(night.get("enddate"), night.get("timezone")),
            "score": d.get("sleep_score"),
            "asleep_min": _minutes(d.get("total_sleep_time")),
            "in_bed_min": _minutes(d.get("total_timeinbed")),
            "efficiency": d.get("sleep_efficiency"),
            "deep_min": _minutes(d.get("deepsleepduration")),
            "light_min": _minutes(d.get("lightsleepduration")),
            "rem_min": _minutes(d.get("remsleepduration")),
            "awake_min": _minutes(d.get("wakeupduration")),
            "time_to_sleep_min": _minutes(d.get("sleep_latency")),
            "time_to_wake_min": _minutes(d.get("wakeup_latency")),
            "wakeups": d.get("wakeupcount"),
            "out_of_bed": d.get("out_of_bed_count"),
            "hr_average": d.get("hr_average"),
            "hr_min": d.get("hr_min"),
            "hr_max": d.get("hr_max"),
            "rr_average": d.get("rr_average"),
            "snoring_min": _minutes(d.get("snoring")),
            "snoring_episodes": d.get("snoringepisodecount"),
            "apnea_hypopnea_index": d.get("apnea_hypopnea_index"),
            "breathing_disturbances": d.get("breathing_disturbances_intensity"),
        }))
    nights.sort(key=lambda n: n.get("date") or "", reverse=True)
    return {"nights": nights}


@mcp.tool()
async def withings_workouts(start: str | None = None, end: str | None = None) -> dict:
    """Séances de sport enregistrées par Withings (marche, course, vélo, natation…).

    Une entrée par séance : discipline, début/fin, durée, calories, distance,
    fréquence cardiaque. Distinct de `withings_activity`, qui agrège la journée.

    start / end : bornes AAAA-MM-JJ. Par défaut, les 30 derniers jours.
    """
    window = _window(start, end, default_days=30)
    if isinstance(window, str):
        return {"error": window}
    begin, stop = window

    result = await _call("v2/measure", {
        "action": "getworkouts",
        "startdateymd": begin.date().isoformat(),
        "enddateymd": stop.date().isoformat(),
        "data_fields": ",".join(WORKOUT_FIELDS),
    })
    if "error" in result:
        return result

    sessions = []
    for session in result["body"].get("series") or []:
        d = session.get("data") or {}
        category = session.get("category")
        start_epoch, end_epoch = session.get("startdate"), session.get("enddate")
        duration = (_minutes(end_epoch - start_epoch)
                    if start_epoch and end_epoch else None)
        sessions.append(_compact({
            "date": session.get("date"),
            "activity": WORKOUT_CATEGORIES.get(category, f"catégorie {category}"),
            "start": _local(start_epoch, session.get("timezone")),
            "end": _local(end_epoch, session.get("timezone")),
            "duration_min": duration,
            "calories": d.get("calories"),
            "distance_m": round(d["distance"]) if d.get("distance") else None,
            "elevation_m": d.get("elevation"),
            "steps": d.get("steps"),
            "hr_average": d.get("hr_average"),
            "hr_max": d.get("hr_max"),
            "intensity": d.get("intensity"),
        }))
    sessions.sort(key=lambda s: s.get("start") or "", reverse=True)
    return {"workouts": sessions}


@mcp.tool()
async def withings_devices() -> dict:
    """Les appareils Withings du compte : type, modèle, niveau de pile, dernière synchro.

    C'est ici qu'on répond à « la balance a-t-elle encore de la pile ? » ou « depuis
    quand la montre n'a-t-elle plus rien remonté ? ».
    """
    result = await _call("v2/user", {"action": "getdevice"})
    if "error" in result:
        return result

    devices = []
    for device in result["body"].get("devices") or []:
        devices.append(_compact({
            "type": device.get("type"),
            "model": device.get("model"),
            "battery": device.get("battery"),
            "last_session": _local(device.get("last_session_date")),
            "first_session": _local(device.get("first_session_date")),
            "device_id": device.get("deviceid"),
        }))
    devices.sort(key=lambda d: d.get("last_session") or "", reverse=True)
    return {"devices": devices}


# --------------------------------------------------------------------------
# Enrolment (browser flow, guarded by the ingress forwardAuth)
# --------------------------------------------------------------------------

def _state_key() -> bytes:
    path = os.path.join(_data_dir(), "state.key")
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        os.makedirs(_data_dir(), exist_ok=True)
        with open(path, "wb") as f:
            f.write(key)
        os.chmod(path, 0o600)
        return key


def _sign_state(sub: str) -> str:
    payload = f"{int(time.time()) + 600}.{sub}"
    sig = hmac.new(_state_key(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()


def _verify_state(state: str) -> str | None:
    try:
        expiry, sub, sig = base64.urlsafe_b64decode(state.encode()).decode().split(".", 2)
        payload = f"{expiry}.{sub}"
        if not hmac.compare_digest(
            sig, hmac.new(_state_key(), payload.encode(), hashlib.sha256).hexdigest()
        ):
            return None
        if time.time() > int(expiry):
            return None
        return sub
    except Exception:
        return None


def _page(glyph: str, title: str, message: str, status: int = 200):
    return enrol_page("Withings", glyph, title, message, status)


def _remote_user(request) -> str | None:
    # Set by the Authelia forwardAuth in front of these paths (ingress-level).
    value = request.headers.get("Remote-User")
    if value is None:
        return None
    # Headers are latin-1 on the wire while Authelia emits UTF-8: recover the
    # accents, then NFC so the credential key is stable.
    try:
        value = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return unicodedata.normalize("NFC", value)


def _redirect_uri() -> str:
    external = os.environ.get("ROSETTA_EXTERNAL_URL", "")
    return f"{external.rstrip('/')}/withings/callback"


async def enroll(request):
    sub = _remote_user(request)
    if not sub:
        return _page("🚪", "Accès refusé",
                     "Cette page passe par le SSO de la maison — pas par la porte de service.", 403)
    client = _oauth_client()
    if isinstance(client, str):
        return _page("🧩", "Configuration absente", client, 500)
    params = {
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": _redirect_uri(),
        # Comma-separated: Withings rejects the space-separated OAuth form.
        "scope": ",".join(SCOPES),
        "state": _sign_state(sub),
    }
    return RedirectResponse(f"{AUTH_URL}?{urlencode(params)}", status_code=302)


async def callback(request):
    state = request.query_params.get("state", "")
    code = request.query_params.get("code")
    sub = _verify_state(state)
    if not sub or not code:
        return _page("⏳", "Flux invalide ou expiré",
                     "Reprendre depuis /withings/enroll — le lien n'est valable que dix minutes.",
                     400)
    client = _oauth_client()
    if isinstance(client, str):
        return _page("🧩", "Configuration absente", client, 500)
    async with _client() as http:
        r = await http.post(TOKEN_URL, data={
            "action": "requesttoken",
            "grant_type": "authorization_code",
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "code": code,
            "redirect_uri": _redirect_uri(),
        })
        try:
            data = r.json()
        except ValueError:
            data = {}
    # HTTP 200 with status != 0 is the normal Withings failure shape.
    body = data.get("body") or {}
    if data.get("status") != STATUS_OK or not body.get("refresh_token"):
        detail = data.get("error") or f"status {data.get('status', r.status_code)}"
        return _page("🛑", "Échange refusé par Withings",
                     f"Détail : {detail}. Reprendre depuis /withings/enroll.", 502)
    _write_user(sub, {
        "sub": sub,
        "userid": body.get("userid"),
        "refresh_token": body["refresh_token"],
        "scopes": (body.get("scope") or "").replace(",", " ").split(),
        "enrolled_at": int(time.time()),
    })
    _token_cache.pop(sub, None)
    return _page("⚖️", "Compte enrôlé",
                 f"Le compte Withings de <b>{sub}</b> est désormais au service de la maison. "
                 "Cette page peut être fermée.")


extra_routes = [("/enroll", enroll, ["GET"]), ("/callback", callback, ["GET"])]
open_paths = ["/enroll", "/callback"]


if __name__ == "__main__":
    # Local stdio debugging: `python -m rosetta.addons.withings`.
    mcp.run()
