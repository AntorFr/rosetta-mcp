"""`transit` addon - SNCF and RATP/IDFM live status, read-only, on demand.

Both providers expose the same Navitia API (same endpoints, same response
schema) - only the base URL and the auth mechanism differ:
  - SNCF (api.sncf.com): Basic Auth (key as login, empty password).
  - IDFM/PRIM (prim.iledefrance-mobilites.fr/marketplace/v2/navitia, confirmed
    by the official swagger): `apikey` header. Covers metro, RER, bus, tram.

Tools (descriptions intentionally in French - see README):
  - train_departures  / metro_departures  : next departures from a station,
                         searched by name, with computed delay (scheduled vs realtime).
  - train_disruptions / metro_disruptions : ongoing disruptions (strike, works,
                         incident), optionally filtered by station.
"""

import logging
import os
from datetime import datetime

import httpx

from ._common import TIMEOUT, dig, new_server

logging.getLogger("httpx").setLevel(logging.WARNING)

required_env = ["SNCF_API_KEY", "IDFM_API_KEY"]

mcp = new_server("transit")


class Provider:
    def __init__(self, name: str, base_url: str, key_env: str, key_missing_msg: str):
        self.name = name
        self.base_url = base_url
        self.key_env = key_env
        self.key_missing_msg = key_missing_msg

    @property
    def key(self) -> str:
        # Read per call, not at import: the addon loads (and reports itself
        # degraded) even when the key is absent from the environment.
        return os.environ.get(self.key_env, "")

    def need_key(self) -> str | None:
        return None if self.key else self.key_missing_msg

    def request_kwargs(self) -> dict:
        """httpx kwargs authenticating a request for this provider."""
        if self.name == "sncf":
            return {"auth": (self.key, "")}
        return {"headers": {"apikey": self.key}}


SNCF = Provider(
    "sncf", "https://api.sncf.com/v1/coverage/sncf",
    "SNCF_API_KEY", "SNCF_API_KEY absente de l'environnement : clé Navitia non fournie au serveur.",
)
IDFM = Provider(
    "idfm", "https://prim.iledefrance-mobilites.fr/marketplace/v2/navitia",
    "IDFM_API_KEY", "IDFM_API_KEY absente de l'environnement : clé PRIM non fournie au serveur.",
)


def _delay_minutes(theoretical: str | None, realtime: str | None) -> int | None:
    """Minutes between two Navitia datetimes (YYYYMMDDTHHMMSS)."""
    if not theoretical or not realtime:
        return None
    if theoretical == realtime:
        return 0
    fmt = "%Y%m%dT%H%M%S"
    try:
        t, r = datetime.strptime(theoretical, fmt), datetime.strptime(realtime, fmt)
    except ValueError:
        return None
    return round((r - t).total_seconds() / 60)


async def _resolve_stop_area(client: httpx.AsyncClient, provider: Provider, station: str) -> dict | str:
    """Station name -> first matching stop_area. Error as str on failure."""
    r = await client.get(
        f"{provider.base_url}/places",
        params={"q": station, "type[]": "stop_area", "count": 1},
        **provider.request_kwargs(),
    )
    if r.status_code != 200:
        return f"recherche impossible pour « {station} » : HTTP {r.status_code}"
    places = r.json().get("places") or []
    if not places:
        return f"aucun arrêt trouvé pour « {station} »."
    top = places[0]
    return {"id": dig(top, "stop_area", "id"), "name": dig(top, "stop_area", "name", default=top.get("name"))}


async def _departures(provider: Provider, station: str, count: int) -> dict:
    if err := provider.need_key():
        return {"error": err}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        stop = await _resolve_stop_area(client, provider, station)
        if isinstance(stop, str):
            return {"error": stop}
        r = await client.get(
            f"{provider.base_url}/stop_areas/{stop['id']}/departures",
            params={"count": count, "data_freshness": "realtime"},
            **provider.request_kwargs(),
        )
        data = r.json()
    if r.status_code != 200:
        return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
    out = []
    for dep in data.get("departures") or []:
        sdt = dep.get("stop_date_time") or {}
        theo, real = sdt.get("base_departure_date_time"), sdt.get("departure_date_time")
        delay = _delay_minutes(theo, real)
        out.append({
            "line": dig(dep, "display_informations", "label", default=dig(dep, "display_informations", "code")),
            "direction": dig(dep, "display_informations", "direction"),
            "network": dig(dep, "display_informations", "network"),
            "scheduled": theo,
            "realtime": real,
            "delay_min": delay,
            "status": "retardé" if (delay or 0) > 0 else ("à l'heure" if delay is not None else "inconnu"),
        })
    return {"station": stop["name"], "departures": out}


async def _disruptions(provider: Provider, station: str | None) -> dict:
    if err := provider.need_key():
        return {"error": err}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        if station:
            stop = await _resolve_stop_area(client, provider, station)
            if isinstance(stop, str):
                return {"error": stop}
            url, label = f"{provider.base_url}/stop_areas/{stop['id']}/disruptions", stop["name"]
        else:
            url, label = f"{provider.base_url}/disruptions", None
        r = await client.get(url, **provider.request_kwargs())
        data = r.json()
    if r.status_code != 200:
        return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
    out = []
    for d in data.get("disruptions") or []:
        out.append({
            "title": dig(d, "messages", 0, "text", default=d.get("disruption_id") or d.get("id")),
            "severity": dig(d, "severity", "name"),
            "effect": dig(d, "severity", "effect"),
            "status": d.get("status"),
        })
    return {"station": label, "disruptions": out}


@mcp.tool()
async def train_departures(station: str, count: int = 5) -> dict:
    """Prochains départs SNCF depuis une gare, avec retard calculé (théorique vs temps réel).

    station : nom de la gare (ex. « Nantes », « Paris Montparnasse »).
    count : nombre de départs à retourner (défaut 5).
    """
    return await _departures(SNCF, station, count)


@mcp.tool()
async def train_disruptions(station: str | None = None) -> dict:
    """Perturbations SNCF en cours (grève, travaux, incident). Filtrable par gare.

    station : nom de gare pour restreindre aux perturbations qui l'affectent ;
              omis = toutes les perturbations réseau en cours.
    """
    return await _disruptions(SNCF, station)


@mcp.tool()
async def metro_departures(station: str, count: int = 5) -> dict:
    """Prochains passages RATP/IDFM (métro, RER, bus, tram) sur une station, avec retard calculé.

    station : nom de la station (ex. « Châtelet », « Nation »).
    count : nombre de passages à retourner (défaut 5).
    """
    return await _departures(IDFM, station, count)


@mcp.tool()
async def metro_disruptions(station: str | None = None) -> dict:
    """Perturbations RATP/IDFM en cours (incident, travaux). Filtrable par station.

    station : nom de station pour restreindre aux perturbations qui l'affectent ;
              omis = toutes les perturbations réseau en cours.
    """
    return await _disruptions(IDFM, station)


if __name__ == "__main__":
    # Local stdio debugging: `python -m rosetta.addons.transit`.
    mcp.run()
