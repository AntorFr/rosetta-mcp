"""`maps` addon - Google Maps Platform behind a single key (`GOOGLE_MAPS_API_KEY`).

Tools (descriptions intentionally in French - they are runtime UX for
French-speaking agents, see README):
  - travel_time      : door-to-door duration + distance, live traffic (Routes API)
  - search_places    : free-text place search (Places API New, Text Search)
  - geocode          : address -> coordinates + normalized address
  - weather_now      : current conditions (Weather API)
  - weather_forecast : daily forecast (Weather API)

APIs to enable on the key: Routes, Places (New), Weather. (No Geocoding API
needed: address resolution goes through Places, which returns the location.
The key travels in a HEADER, never in the query string - except for the
Weather API, which only accepts a query param; the httpx INFO log is silenced
so the key never leaks into logs.)
A location is always accepted either spelled out ("12 rue X, Nantes") or as
"lat,lng"; the weather tools resolve addresses as needed.
"""

import logging
import os
import re

import httpx

from ._common import TIMEOUT, dig, new_server

logging.getLogger("httpx").setLevel(logging.WARNING)

required_env = ["GOOGLE_MAPS_API_KEY"]

LANG = "fr"
REGION = "FR"

mcp = new_server("maps")

_LATLNG = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


def _api_key() -> str:
    # Read per call, not at import: the addon loads (and reports itself
    # degraded) even when the key is absent from the environment.
    return os.environ.get("GOOGLE_MAPS_API_KEY", "")


def _need_key() -> str | None:
    if not _api_key():
        return "GOOGLE_MAPS_API_KEY absente de l'environnement : la clé n'est pas fournie au serveur."
    return None


async def _geocode(client: httpx.AsyncClient, address: str) -> dict | str:
    """Address/place -> {lat, lng, formatted, place_id} via Places Text Search.
    The key goes in a HEADER (no query-string leak). Returns an error str on failure."""
    r = await client.post(
        "https://places.googleapis.com/v1/places:searchText",
        headers={
            "X-Goog-Api-Key": _api_key(),
            "X-Goog-FieldMask": "places.location,places.formattedAddress,places.id,places.displayName",
            "Content-Type": "application/json",
        },
        json={"textQuery": address, "maxResultCount": 1, "languageCode": LANG, "regionCode": REGION},
    )
    data = r.json()
    if r.status_code != 200:
        return f"résolution impossible pour « {address} » : {dig(data, 'error', 'message', default=r.status_code)}"
    places = data.get("places") or []
    if not places:
        return f"aucun lieu trouvé pour « {address} »."
    top = places[0]
    loc = top.get("location") or {}
    return {
        "lat": loc.get("latitude"),
        "lng": loc.get("longitude"),
        "formatted": top.get("formattedAddress") or dig(top, "displayName", "text"),
        "place_id": top.get("id"),
    }


async def _resolve_latlng(client: httpx.AsyncClient, place: str):
    """"lat,lng" -> (lat, lng) directly; anything else is geocoded.
    Returns (lat, lng, label) or an error str."""
    m = _LATLNG.match(place)
    if m:
        return float(m.group(1)), float(m.group(2)), place
    g = await _geocode(client, place)
    if isinstance(g, str):
        return g
    return g["lat"], g["lng"], g["formatted"]


def _waypoint(place: str) -> dict:
    """Routes waypoint: coordinates when "lat,lng", plain address otherwise (Routes geocodes)."""
    m = _LATLNG.match(place)
    if m:
        return {"location": {"latLng": {"latitude": float(m.group(1)), "longitude": float(m.group(2))}}}
    return {"address": place}


def _fmt_duration(dur: str | None) -> str:
    """Routes returns durations like "1830s"."""
    if not dur:
        return "?"
    s = int(str(dur).rstrip("s"))
    h, rem = divmod(s, 3600)
    m = rem // 60
    return f"{h} h {m:02d}" if h else f"{m} min"


@mcp.tool()
async def travel_time(origin: str, destination: str, mode: str = "DRIVE", depart_at: str | None = None) -> dict:
    """Temps de trajet et distance entre deux lieux, trafic réel pris en compte.

    origin / destination : adresse en toutes lettres ou « lat,lng ».
    mode : DRIVE (défaut), WALK, BICYCLE, TRANSIT, TWO_WHEELER.
    depart_at : heure de départ ISO 8601 (ex. 2026-07-20T08:30:00+02:00), future.
                Omise = maintenant (trafic actuel pour DRIVE).
    """
    if err := _need_key():
        return {"error": err}
    mode = (mode or "DRIVE").upper()
    body = {
        "origin": _waypoint(origin),
        "destination": _waypoint(destination),
        "travelMode": mode,
        "languageCode": LANG,
        "regionCode": REGION,
    }
    # routingPreference/departureTime are only valid for DRIVE/TWO_WHEELER (traffic).
    if mode in ("DRIVE", "TWO_WHEELER"):
        body["routingPreference"] = "TRAFFIC_AWARE"
        if depart_at:
            body["departureTime"] = depart_at
    elif mode == "TRANSIT" and depart_at:
        body["departureTime"] = depart_at

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.post(
            "https://routes.googleapis.com/directions/v2:computeRoutes",
            headers={
                "X-Goog-Api-Key": _api_key(),
                "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
                "Content-Type": "application/json",
            },
            json=body,
        )
        data = r.json()
    if r.status_code != 200:
        return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
    routes = data.get("routes") or []
    if not routes:
        return {"error": "aucun itinéraire trouvé entre ces deux points."}
    top = routes[0]
    meters = top.get("distanceMeters") or 0
    return {
        "origin": origin,
        "destination": destination,
        "mode": mode,
        "duration": _fmt_duration(top.get("duration")),
        "distance_km": round(meters / 1000, 1),
    }


@mcp.tool()
async def search_places(query: str, near: str | None = None, max_results: int = 5) -> dict:
    """Recherche de lieux (restaurants, hôtels, sites…) par texte libre.

    query : ce qu'on cherche (« restaurant italien », « pharmacie de garde »…).
    near : optionnel, un lieu de contexte (« Nantes », « près de la gare »).
    max_results : nombre de résultats (défaut 5).
    """
    if err := _need_key():
        return {"error": err}
    text = f"{query} près de {near}" if near else query
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.post(
            "https://places.googleapis.com/v1/places:searchText",
            headers={
                "X-Goog-Api-Key": _api_key(),
                "X-Goog-FieldMask": (
                    "places.displayName,places.formattedAddress,places.location,"
                    "places.id,places.rating,places.userRatingCount,"
                    "places.currentOpeningHours.openNow,places.primaryTypeDisplayName"
                ),
                "Content-Type": "application/json",
            },
            json={"textQuery": text, "maxResultCount": max_results, "languageCode": LANG, "regionCode": REGION},
        )
        data = r.json()
    if r.status_code != 200:
        return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
    out = []
    for p in data.get("places") or []:
        loc = p.get("location") or {}
        out.append({
            "name": dig(p, "displayName", "text"),
            "address": p.get("formattedAddress"),
            "type": dig(p, "primaryTypeDisplayName", "text"),
            "rating": p.get("rating"),
            "ratings_count": p.get("userRatingCount"),
            "open_now": dig(p, "currentOpeningHours", "openNow"),
            "latlng": f"{loc.get('latitude')},{loc.get('longitude')}" if loc else None,
            "place_id": p.get("id"),
        })
    return {"query": text, "results": out}


@mcp.tool()
async def geocode(address: str) -> dict:
    """Convertit une adresse en coordonnées + adresse normalisée (utile pour désambiguïser)."""
    if err := _need_key():
        return {"error": err}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        g = await _geocode(client, address)
    if isinstance(g, str):
        return {"error": g}
    return g


async def _weather_common(location: str):
    """Resolve a location for the weather tools. (client, lat, lng, label) or error str."""
    client = httpx.AsyncClient(timeout=TIMEOUT)
    res = await _resolve_latlng(client, location)
    if isinstance(res, str):
        await client.aclose()
        return res
    lat, lng, label = res
    return client, lat, lng, label


@mcp.tool()
async def weather_now(location: str) -> dict:
    """Conditions météo actuelles pour un lieu (adresse en clair ou « lat,lng »)."""
    if err := _need_key():
        return {"error": err}
    common = await _weather_common(location)
    if isinstance(common, str):
        return {"error": common}
    client, lat, lng, label = common
    try:
        r = await client.get(
            "https://weather.googleapis.com/v1/currentConditions:lookup",
            params={"key": _api_key(), "location.latitude": lat, "location.longitude": lng,
                    "unitsSystem": "METRIC", "languageCode": LANG},
        )
        data = r.json()
    finally:
        await client.aclose()
    if r.status_code != 200:
        return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
    return {
        "location": label,
        "condition": dig(data, "weatherCondition", "description", "text"),
        "temp_c": dig(data, "temperature", "degrees"),
        "feels_like_c": dig(data, "feelsLikeTemperature", "degrees"),
        "humidity_pct": data.get("relativeHumidity"),
        "wind_kmh": dig(data, "wind", "speed", "value"),
        "wind_dir": dig(data, "wind", "direction", "cardinal"),
        "precip_prob_pct": dig(data, "precipitation", "probability", "percent"),
        "uv_index": data.get("uvIndex"),
    }


@mcp.tool()
async def weather_forecast(location: str, days: int = 5) -> dict:
    """Prévisions météo journalières (1 à 10 jours) pour un lieu (adresse ou « lat,lng »)."""
    if err := _need_key():
        return {"error": err}
    days = max(1, min(int(days), 10))
    common = await _weather_common(location)
    if isinstance(common, str):
        return {"error": common}
    client, lat, lng, label = common
    try:
        r = await client.get(
            "https://weather.googleapis.com/v1/forecast/days:lookup",
            params={"key": _api_key(), "location.latitude": lat, "location.longitude": lng,
                    "days": days, "unitsSystem": "METRIC", "languageCode": LANG},
        )
        data = r.json()
    finally:
        await client.aclose()
    if r.status_code != 200:
        return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
    out = []
    for d in data.get("forecastDays") or []:
        disp = d.get("displayDate") or {}
        out.append({
            "date": f"{disp.get('year')}-{disp.get('month'):02d}-{disp.get('day'):02d}"
                    if disp.get("year") else None,
            "condition": dig(d, "daytimeForecast", "weatherCondition", "description", "text"),
            "temp_min_c": dig(d, "minTemperature", "degrees"),
            "temp_max_c": dig(d, "maxTemperature", "degrees"),
            "precip_prob_pct": dig(d, "daytimeForecast", "precipitation", "probability", "percent"),
        })
    return {"location": label, "days": out}


if __name__ == "__main__":
    # Local stdio debugging: `python -m rosetta.addons.maps`.
    mcp.run()
