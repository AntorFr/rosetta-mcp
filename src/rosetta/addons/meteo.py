"""`meteo` addon - Open-Meteo, the wind as a dinghy sailor needs it. Read only.

Tools (descriptions intentionally in French - they are runtime UX for
French-speaking agents, see README):
  - wind_forecast : hour-by-hour wind for a spot - mean, GUST and bearing, in
                    knots, bounded to daylight, optionally across several models
  - wind_spots    : the named-spot registry, so the agent knows what it may name

No key, no account, no enrolment, no volume: Open-Meteo serves forecasts
anonymously, so this addon carries no secret and stays `identity = "machine"`,
exactly like `food`. Attribution is not optional though - the data is CC-BY 4.0,
so every answer names its source.

WHY NOT the `maps` addon, which already speaks weather: Google's Weather API is
MetNet, one closed model, no model choice. For planning a sail, what settles the
question is the gust alongside the mean and whether the models AGREE - AROME
France HD resolves 1.5 km where a global model sees 25 km, and a coastal breeze
lives well inside that gap. `maps` answers "what is the weather"; this answers
"can I go out".

FOUR TRAPS, all measured against the live API on 2026-08-01, not assumed:

1. ⚠️ **A model outside its domain is DROPPED from a multi-model response** -
   no error, no null column, HTTP 200. Asking for
   `meteofrance_arome_france_hd,ecmwf_ifs025` over Quebec returns ECMWF's
   numbers alone (byte-identical to asking ECMWF on its own), with AROME simply
   absent. Asked *alone* it is honest - HTTP 400, "No data is available for this
   location" - so the danger is exactly the batched call.
2. ⚠️ **The response key is suffixed by the number of SURVIVORS, not by what
   was asked.** Two models requested, one back, and the key is the bare
   `wind_speed_10m`. A parser looking for `wind_speed_10m_ecmwf_ifs025` finds
   nothing; a naive one reads the bare key and files ECMWF's numbers under
   AROME HD - a silent lie about the source, in the one feature whose whole
   point is knowing who said what.

   Hence the design here: **ONE REQUEST PER MODEL, never a batched one.** The
   key is then always bare, "no data" is always an explicit 400, and traps 1
   and 2 stop existing rather than being worked around. It costs N HTTP calls
   against a 600/minute budget, which is not a price worth haggling over.
3. Past its **horizon** the same model does the opposite - it returns `null`
   rows rather than vanishing. Two different shapes for "no data". AROME France
   HD measured 69 h of real rows where the docs advertise "2 days", so the
   horizon is read off the data, never hardcoded.
4. **The geocoder will drown you.** `name=La Torche` resolves to a hamlet in
   the Allier - 46.28, 2.77, elevation 367 m, 400 km from any sea - with no
   error and a perfectly plausible wind. That is the entire justification for
   the spot registry: the names a sailor uses (a headland, a club slipway, a
   lake's north shore) are precisely the ones a geocoder gets wrong. So a
   geocoded spot always comes back flagged, with its elevation and region
   attached, and the registry is consulted first.

Rate limit: Open-Meteo allows 600 calls/minute, 10 000/day for non-commercial
use, counted per IP - so per deployment, shared with the whole house, like Open
Food Facts. No in-process quota here though: `food` needed one because its
budget is 15/minute, forty times tighter, whereas a sail is planned in a handful
of calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from ._common import TIMEOUT, new_server

logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("rosetta.meteo")

mcp = new_server("meteo")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
SOURCE = "Open-Meteo (open-meteo.com), CC-BY 4.0"

# Test seam, same as `food` and `maps`.
_transport = None

_LATLNG = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Short names an agent can actually say, mapped to Open-Meteo ids. Resolutions
# and horizons are the upstream figures; the horizon actually served is read off
# the data (trap 3), never from this table.
MODELS = {
    "auto": "best_match",            # Open-Meteo picks the finest available
    "arome_hd": "meteofrance_arome_france_hd",   # ~1.5 km, France, ~2 days
    "arome": "meteofrance_arome_france",         # ~2.5 km, France, ~2 days
    "arpege": "meteofrance_arpege_europe",       # ~11 km, Europe, ~4 days
    "arpege_monde": "meteofrance_arpege_world",  # ~25 km, global, ~4 days
    "ecmwf": "ecmwf_ifs025",                     # ~25 km, global, 15 days
    "icon": "icon_eu",                           # ~7 km, Europe
    "gfs": "gfs_seamless",                       # global
}

MAX_MODELS = 5


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, transport=_transport)


def _tz() -> str:
    """The HOUSE zone - used only to decide what "today" means when no day is
    given. The forecasts themselves are asked for in the SPOT's own zone."""
    return os.environ.get("TZ") or "Europe/Paris"


def _today() -> str:
    return datetime.now(ZoneInfo(_tz())).strftime("%Y-%m-%d")


def _norm(text: str) -> str:
    """Fold case and accents so "La Torche", "la torche" and "LA TORCHÉ" are one
    key. A spot is typed by a human, in a hurry, from a phone."""
    folded = unicodedata.normalize("NFD", text.strip().lower())
    return "".join(c for c in folded if not unicodedata.combining(c))


def _spots() -> dict:
    """The named spots, from `ROSETTA_WIND_SPOTS` (JSON).

    Read per call, not at import, so adding a spot is a rollout and not an
    image rebuild. A malformed value is logged and ignored rather than taking
    the addon down: a typo in one spot must not cost the other nine.

    Accepted shapes, per entry:
        "La Torche": "47.8367,-4.3492"
        "La Torche": {"latlng": "47.8367,-4.3492", "note": "cale nord"}
    """
    raw = os.environ.get("ROSETTA_WIND_SPOTS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("ROSETTA_WIND_SPOTS n'est pas du JSON valide (%s) : registre ignoré", exc)
        return {}
    if not isinstance(parsed, dict):
        logger.warning("ROSETTA_WIND_SPOTS doit être un objet JSON : registre ignoré")
        return {}

    spots = {}
    for name, value in parsed.items():
        entry = {"latlng": value} if isinstance(value, str) else dict(value or {})
        latlng = str(entry.get("latlng", ""))
        if not _LATLNG.match(latlng):
            logger.warning("spot « %s » : « %s » n'est pas des coordonnées, ignoré", name, latlng)
            continue
        spots[name] = entry
    return spots


async def _geocode(client: httpx.AsyncClient, place: str) -> dict | str:
    """Free-text place -> coordinates, via Open-Meteo's own geocoder.

    The result is ALWAYS returned with its region and elevation attached and a
    warning flag: this is trap 4, and the only defence against it is making the
    guess visible at the point of use (see the module docstring - "La Torche"
    lands in the Allier, at 367 m).
    """
    r = await client.get(GEOCODING_URL,
                         params={"name": place, "count": 1, "language": "fr", "format": "json"})
    if r.status_code != 200:
        return f"géocodage impossible pour « {place} » : HTTP {r.status_code}."
    hits = (r.json() or {}).get("results") or []
    if not hits:
        return (f"aucun lieu trouvé pour « {place} ». Donner des coordonnées « lat,lng », "
                f"ou inscrire le spot au registre (ROSETTA_WIND_SPOTS).")
    top = hits[0]
    region = ", ".join(str(top[k]) for k in ("admin2", "admin1", "country") if top.get(k))
    return {
        "nom": top.get("name"),
        "lat": top.get("latitude"),
        "lng": top.get("longitude"),
        "resolution": "geocodage",
        "lieu_resolu": f"{top.get('name')} ({region})" if region else top.get("name"),
        "altitude_m": top.get("elevation"),
        "avertissement": ("lieu DEVINÉ par géocodage, pas inscrit au registre — vérifier "
                          "que la région et l'altitude correspondent bien au plan d'eau visé."),
    }


async def _resolve(client: httpx.AsyncClient, spot: str) -> dict | str:
    """Spot -> coordinates. Registry first, then "lat,lng", then the geocoder."""
    if not (spot or "").strip():
        return "aucun spot demandé."

    registry = _spots()
    wanted = _norm(spot)
    for name, entry in registry.items():
        if _norm(name) == wanted:
            lat, lng = _LATLNG.match(entry["latlng"]).groups()
            resolved = {"nom": name, "lat": float(lat), "lng": float(lng),
                        "resolution": "registre"}
            if entry.get("note"):
                resolved["note"] = entry["note"]
            return resolved

    if m := _LATLNG.match(spot):
        return {"nom": spot, "lat": float(m.group(1)), "lng": float(m.group(2)),
                "resolution": "coordonnees"}

    return await _geocode(client, spot)


async def _daylight(client: httpx.AsyncClient, lat: float, lng: float, day: str):
    """(first_hour, last_hour) of daylight, or None if the API will not say.

    Fetched without any `models` parameter: sunrise is astronomy, identical
    whichever model is asked, and keeping it out of the per-model calls keeps
    those responses to a single shape.

    ⚠️ `timezone=auto` is load-bearing, not tidiness. Pinning the house zone
    onto a spot in another one makes sunset land on the FOLLOWING calendar day
    (Quebec sets after midnight, Paris time), so the window inverts and
    collapses to nothing. Measured, not imagined.
    """
    r = await client.get(FORECAST_URL, params={
        "latitude": lat, "longitude": lng, "daily": "sunrise,sunset",
        "timezone": "auto", "start_date": day, "end_date": day,
    })
    if r.status_code != 200:
        return None
    daily = (r.json() or {}).get("daily") or {}
    try:
        sunrise, sunset = daily["sunrise"][0], daily["sunset"][0]
        rise = datetime.fromisoformat(sunrise)
        set_ = datetime.fromisoformat(sunset)
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    # Round inwards: a sail is rigged in full daylight, not in the minute the
    # sun clears the horizon.
    first = rise.hour + (1 if rise.minute else 0)
    last = set_.hour
    # A window that came out backwards means the zone assumption failed; a full
    # day is a poor answer, an empty one is a wrong answer.
    return (first, last) if first <= last else None


def _mean_bearing(degrees: list) -> int | None:
    """Circular mean of bearings.

    ⚠️ NOT an arithmetic mean: averaging 350° and 10° that way yields 180°, the
    exact opposite of the truth, which on a shore-break spot is the difference
    between onshore and offshore. Sum the unit vectors instead.
    """
    if not degrees:
        return None
    x = sum(math.cos(math.radians(d)) for d in degrees)
    y = sum(math.sin(math.radians(d)) for d in degrees)
    if abs(x) < 1e-9 and abs(y) < 1e-9:
        return None  # perfectly opposed bearings have no mean, and saying so beats inventing one
    # Round THEN wrap, not the reverse: a mean of 350° and 10° lands on
    # 359.99999°, which `% 360` leaves alone and `round` then lifts to 360 - a
    # bearing that does not exist. Caught by the test, not by reading the code.
    return round(math.degrees(math.atan2(y, x)) % 360) % 360


_CARDINALS_FR = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                 "S", "SSO", "SO", "OSO", "O", "ONO", "NO", "NNO")


def _cardinal_fr(degrees) -> str | None:
    """Bearing -> 16-point French cardinal. Where the wind blows FROM."""
    if degrees is None:
        return None
    return _CARDINALS_FR[round(float(degrees) / 22.5) % 16]


def _series(hourly: dict, model_id: str, first: int, last: int) -> list:
    """Open-Meteo `hourly` block -> the rows inside the window.

    Reads the bare key, with the suffixed one as a fallback: one model per
    request means the key is always bare (trap 2), and the fallback is there so
    a future batched call fails loudly rather than silently mis-attributing.

    Rows whose wind is `null` are DROPPED, not zeroed: past a model's horizon
    Open-Meteo returns nulls (trap 3), and a null is "unknown", never "calm".
    """
    def column(name):
        return hourly.get(name) or hourly.get(f"{name}_{model_id}") or []

    times = hourly.get("time") or []
    speeds, gusts, dirs = column("wind_speed_10m"), column("wind_gusts_10m"), column("wind_direction_10m")

    rows = []
    for i, stamp in enumerate(times):
        hour = int(stamp[11:13])
        if not (first <= hour <= last):
            continue
        speed = speeds[i] if i < len(speeds) else None
        if speed is None:
            continue
        gust = gusts[i] if i < len(gusts) else None
        bearing = dirs[i] if i < len(dirs) else None
        row = {"h": stamp[11:16], "vent_kn": speed}
        if gust is not None:
            row["rafale_kn"] = gust
            # The gust factor is what separates a steady breeze from a squally
            # one. Reported raw, with no verdict attached: where the threshold
            # sits depends on the boat and the sailor, not on this addon.
            if speed > 0:
                row["ratio_rafale"] = round(gust / speed, 2)
        if bearing is not None:
            row["dir_deg"] = bearing
            row["dir"] = _cardinal_fr(bearing)
        rows.append(row)
    return rows


def _summary(rows: list) -> dict:
    if not rows:
        return {}
    speeds = [r["vent_kn"] for r in rows]
    gusts = [r["rafale_kn"] for r in rows if "rafale_kn" in r]
    bearing = _mean_bearing([r["dir_deg"] for r in rows if "dir_deg" in r])
    out = {
        "vent_min_kn": min(speeds),
        "vent_max_kn": max(speeds),
        "vent_moyen_kn": round(sum(speeds) / len(speeds), 1),
    }
    if gusts:
        out["rafale_max_kn"] = max(gusts)
    if bearing is not None:
        out["dir_dominante"] = _cardinal_fr(bearing)
        out["dir_dominante_deg"] = bearing
    return out


async def _fetch_model(client: httpx.AsyncClient, lat: float, lng: float, day: str,
                       alias: str, first: int, last: int) -> dict:
    """One model, one request. See trap 1 and 2 in the module docstring."""
    model_id = MODELS.get(alias, alias)
    r = await client.get(FORECAST_URL, params={
        "latitude": lat, "longitude": lng,
        "hourly": "wind_speed_10m,wind_gusts_10m,wind_direction_10m",
        "wind_speed_unit": "kn", "timezone": "auto",
        "start_date": day, "end_date": day, "models": model_id,
    })
    try:
        data = r.json()
    except ValueError:
        return {"modele": alias, "erreur": f"réponse illisible (HTTP {r.status_code})"}

    # Out of domain answers HTTP 400 with an explicit reason - BECAUSE the model
    # was asked on its own. Batched, it would simply have vanished.
    if data.get("error") or r.status_code != 200:
        return {"modele": alias, "id_amont": model_id,
                "erreur": data.get("reason") or f"HTTP {r.status_code}"}

    rows = _series(data.get("hourly") or {}, model_id, first, last)
    result = {"modele": alias, "id_amont": model_id, "fuseau": data.get("timezone"),
              "heures": rows, "resume": _summary(rows)}
    if not rows:
        result["erreur"] = ("aucune donnée sur ce créneau : le jour demandé est probablement "
                            "au-delà de l'horizon de ce modèle.")
    return result


def _dispersion(models: list) -> dict:
    """How far apart the models are over the window - the honest proxy for
    confidence. Only meaningful once at least two of them actually answered."""
    means = {m["modele"]: m["resume"]["vent_moyen_kn"]
             for m in models if m.get("resume", {}).get("vent_moyen_kn") is not None}
    if len(means) < 2:
        return {}
    low, high = min(means.values()), max(means.values())
    return {
        "vent_moyen_par_modele_kn": means,
        "ecart_kn": round(high - low, 1),
        "lecture": ("Écart faible = les modèles s'accordent, prévision solide. Écart large = "
                    "situation incertaine, revoir la veille."),
    }


@mcp.tool()
async def wind_forecast(spot: str, jour: str | None = None, de: int | None = None,
                        a: int | None = None, modeles: str | None = None) -> dict:
    """Prévisions de VENT heure par heure pour un spot, en NŒUDS — vent moyen,
    rafale, ratio de rafale et direction (d'où il vient).

    spot : un nom du registre (voir `wind_spots`), « lat,lng », ou un lieu en
           toutes lettres. ⚠️ En toutes lettres, le lieu est DEVINÉ par
           géocodage : la réponse porte alors un avertissement, la région et
           l'altitude — les vérifier avant de conclure.
    jour : AAAA-MM-JJ (défaut : aujourd'hui).
    de / a : bornes horaires (0-23). Omises, le créneau est le JOUR CLAIR
             (lever/coucher du soleil du lieu).
    modeles : liste séparée par des virgules (5 au maximum). Défaut « auto »
              (Open-Meteo choisit le modèle le plus fin du point).
              Disponibles : auto, arome_hd (~1,5 km, France, ~2 j — le plus fin
              près des côtes), arome, arpege, arpege_monde, ecmwf, icon, gfs.
              En donner plusieurs compare les modèles et rend leur écart : c'est
              la mesure de confiance de la prévision.
    """
    day = (jour or "").strip() or _today()
    if not _DAY.match(day):
        return {"error": f"date « {jour} » illisible : format attendu AAAA-MM-JJ."}

    # An alias that is not in MODELS is passed through as a raw Open-Meteo id:
    # the table is a convenience, not a whitelist, and the API rejects nonsense
    # itself with a 400 that `_fetch_model` carries back verbatim.
    aliases = [a_.strip() for a_ in (modeles or "auto").split(",") if a_.strip()] or ["auto"]
    if len(aliases) > MAX_MODELS:
        return {"error": f"{len(aliases)} modèles demandés, {MAX_MODELS} au maximum."}

    async with _client() as client:
        place = await _resolve(client, spot)
        if isinstance(place, str):
            return {"error": place}
        lat, lng = place["lat"], place["lng"]

        if de is None or a is None:
            window = await _daylight(client, lat, lng, day)
            first, last = window or (0, 23)
            creneau = f"{first:02d}:00–{last:02d}:00 (jour clair)" if window else "00:00–23:00"
        else:
            first, last = max(0, min(int(de), 23)), max(0, min(int(a), 23))
            creneau = f"{first:02d}:00–{last:02d}:00"
        if first > last:
            return {"error": f"créneau vide : « de » ({first}) est après « a » ({last})."}

        results = await asyncio.gather(*[
            _fetch_model(client, lat, lng, day, alias, first, last) for alias in aliases
        ])

    results = list(results)
    # Hours are local to the SPOT, not to the house. Said once, at the top,
    # rather than repeated on every model.
    zone = next((m.pop("fuseau", None) for m in results if m.get("fuseau")), None)
    for model in results:
        model.pop("fuseau", None)

    answer = {
        "spot": place.get("nom"),
        "lieu": place,
        "jour": day,
        "creneau": creneau,
        "fuseau": zone,
        "unite": "nœuds (kn)",
        "modeles": results,
        "source": SOURCE,
    }
    if dispersion := _dispersion(list(results)):
        answer["dispersion"] = dispersion
    return answer


@mcp.tool()
async def wind_spots() -> dict:
    """Les spots de voile enregistrés, avec leurs coordonnées.

    Le registre sert à nommer les lieux qu'un géocodeur rate — une pointe, une
    cale de mise à l'eau, une rive de lac. `wind_forecast` accepte aussi
    « lat,lng » ou un lieu en toutes lettres, mais dans ce dernier cas le lieu
    est deviné et la réponse le signale.
    """
    registry = _spots()
    if not registry:
        return {
            "spots": [],
            "note": ("Aucun spot enregistré (ROSETTA_WIND_SPOTS est vide). "
                     "`wind_forecast` fonctionne quand même avec « lat,lng » ou un nom de lieu."),
        }
    return {"spots": [
        {"nom": name, "latlng": entry["latlng"], **({"note": entry["note"]} if entry.get("note") else {})}
        for name, entry in registry.items()
    ]}


if __name__ == "__main__":
    # Local stdio debugging: `python -m rosetta.addons.meteo`.
    mcp.run()
