"""`marees` addon - French tide times and coefficient, read-only.

Scope is deliberately narrow: **when** the tide turns, and **how strong** it is.
Nothing else. No height curve, no range, no threshold windows, no nautical
route planning. The two questions it answers are "is the foreshore walkable at
three?" and "will the golfe run hard on Saturday?" - and both are answered by a
handful of times and one number.

⚠️ THE COEFFICIENT IS NOT LOCAL. It is computed for the port of **Brest** and
holds identically along the Channel and Atlantic coasts, the tidal wave
reaching them barely distorted. What varies with place is the TIMES, never the
coefficient. The same 100 means ~6 m of range at Brest, over 13 m at
Mont-Saint-Michel, and 0.5 m in the Mediterranean - where the notion is simply
meaningless. Hence the shape of every answer: one coefficient per tide, times
per port.

Source: **api-maree.fr**, which computes water levels from the Ifremer/PREVIMER
harmonic constituents. Free, one account key, 360 requests/hour, and a window
bounded to **J-30 → J+30**.

⚠️ Its coefficient is stated by the source itself as **non-official**. The
authority is the Shom, which sells its SPM/SAPM service - so the figure here is
good enough to decide a walk or a session, and NOT good enough for anything
where the official number is binding. Every answer says so; that label is not
decoration, it is the difference between a suggestion and a claim.

Two guards that exist in the code rather than only in the documentation:
  - the DISTANCE to the port is always returned, and flagged past 25 km. A port
    50 km away does not predict your foreshore;
  - a date outside J±30 is REFUSED rather than extrapolated. The upstream model
    has a window; stepping outside it silently would invent a tide.

Tools (descriptions intentionally in French - see README):
  - marees        : les heures de pleine et basse mer, et le coefficient
  - marees_ports  : les ports connus autour d'un lieu, avec leur distance
"""

import logging
import math
import os
import re
from datetime import date, timedelta

import httpx

from ._common import TIMEOUT, new_server

logging.getLogger("httpx").setLevel(logging.WARNING)

required_env = ["API_MAREE_KEY"]

mcp = new_server("marees")

API = "https://api-maree.fr"
# Le géocodeur de la Géoplateforme : gratuit, sans clé, et il connaît les
# communes françaises — c'est tout ce qu'il faut pour tomber sur le bon port.
IGN_GEOCODE = "https://data.geopf.fr/geocodage/search"
FENETRE_J = 30                     # la fenêtre du modèle amont, en jours
LOIN_KM = 25                       # au-delà, le port ne parle plus tout à fait de votre plage
# Au-delà, il n'en parle plus DU TOUT. Les 131 ports du registre couvrent la
# Manche, l'Atlantique et la mer du Nord — pas un seul en Méditerranée (vérifié
# le 2026-08-06). Sans ce seuil, « Sète » rendait les marées de **Bordeaux**, à
# 389 km et sur une autre mer, avec un simple avertissement « ordre de
# grandeur » — ce qui est faux : ce n'est pas un ordre de grandeur, c'est autre
# chose. Un refus vaut mieux qu'un chiffre poli.
TROP_LOIN_KM = 100

_LATLNG = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Test seam, comme les autres addons : les tests injectent un MockTransport.
_transport = None
# La liste des ports est statique — on ne la retire pas à chaque question.
_sites_cache = None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, transport=_transport)


def _key() -> str:
    return os.environ.get("API_MAREE_KEY", "")


def _need_key() -> str | None:
    if not _key():
        return ("API_MAREE_KEY absente de l'environnement : la clé n'est pas fournie au "
                "serveur (compte gratuit sur api-maree.fr).")
    return None


def _haversine(lat1, lng1, lat2, lng2) -> float:
    """Kilomètres entre deux points."""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


async def _sites(client: httpx.AsyncClient):
    """La liste des ports. Mise en cache : elle ne bouge pas, et la retirer à
    chaque question mangerait le quota pour rien."""
    global _sites_cache
    if _sites_cache is not None:
        return _sites_cache
    try:
        r = await client.get(f"{API}/sites")
        data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        return f"liste des ports injoignable ({type(exc).__name__})."
    sites = data.get("sites") if isinstance(data, dict) else data
    if not sites:
        return "la liste des ports est revenue vide."
    _sites_cache = sites
    return sites


async def _resout(client: httpx.AsyncClient, lieu: str):
    """« lat,lng », un nom de port ou un lieu en toutes lettres -> (lat, lng, libellé)."""
    m = _LATLNG.match(lieu or "")
    if m:
        return float(m.group(1)), float(m.group(2)), lieu
    # Un nom de port d'abord : c'est le cas le plus fréquent et il ne coûte
    # aucun appel réseau supplémentaire.
    sites = await _sites(client)
    if isinstance(sites, str):
        return sites
    cible = (lieu or "").strip().lower()
    for s in sites:
        if cible and (cible == s["site_name"].lower() or cible == s["site_id"]):
            return s["latitude"], s["longitude"], s["site_name"]
    try:
        r = await client.get(IGN_GEOCODE, params={"q": lieu, "limit": 1, "index": "address"})
        feats = (r.json() or {}).get("features") or []
    except (httpx.HTTPError, ValueError):
        feats = []
    if not feats:
        return f"lieu introuvable : « {lieu} »."
    lng, lat = feats[0]["geometry"]["coordinates"]
    return lat, lng, feats[0].get("properties", {}).get("label") or lieu


def _proche(sites, lat, lng):
    """Le port le plus proche, et sa distance."""
    best, bestd = None, float("inf")
    for s in sites:
        d = _haversine(lat, lng, s["latitude"], s["longitude"])
        if d < bestd:
            best, bestd = s, d
    return best, bestd


def _bornes(jour: str | None, jours: int):
    """(début, fin) en ISO, ou un message de refus.

    ⚠️ Le modèle amont ne prédit qu'entre J-30 et J+30. Déborder en silence
    rendrait une marée inventée, ce qui est pire que pas de marée du tout.
    """
    if jour and not _DATE.match(jour):
        return "jour : attendu « AAAA-MM-JJ »."
    debut = date.fromisoformat(jour) if jour else date.today()
    jours = max(1, min(int(jours or 1), 15))
    fin = debut + timedelta(days=jours - 1)
    limite_bas = date.today() - timedelta(days=FENETRE_J)
    limite_haut = date.today() + timedelta(days=FENETRE_J)
    if debut < limite_bas or fin > limite_haut:
        return (f"hors de portée du modèle : les prédictions vont du {limite_bas.isoformat()} "
                f"au {limite_haut.isoformat()} (fenêtre de ±{FENETRE_J} jours).")
    return debut.isoformat(), fin.isoformat()


def _heure(valeur) -> str | None:
    """« 2026-08-07T14:32:00 » ou « 14:32 » -> « 14:32 »."""
    if not valeur:
        return None
    txt = str(valeur)
    m = re.search(r"(\d{1,2}):(\d{2})", txt)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else txt


def _jour(valeur) -> str | None:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", str(valeur or ""))
    return m.group(1) if m else None


def _range(entree: dict) -> dict:
    """Une entrée de l'amont -> une marée, avec son étiquette en français.

    Les noms de champs sont lus avec plusieurs orthographes plausibles : ce
    n'est pas de la superstition, c'est que la réponse n'a pas pu être observée
    (la clé appartient à l'utilisateur). Si rien ne correspond, l'appelant
    reçoit la charge brute plutôt qu'un objet vide — un silence serait
    indébogable.
    """
    def prends(*noms):
        for n in noms:
            if entree.get(n) not in (None, ""):
                return entree[n]
        return None

    brut = str(prends("type", "tide", "extrema", "kind") or "").upper()
    genre = "pleine mer" if brut.startswith("PM") or "HIGH" in brut else (
        "basse mer" if brut.startswith("BM") or "LOW" in brut else brut.lower() or None)
    quand = prends("time", "datetime", "date_time", "heure", "date")
    coef = prends("coefficient", "coeff", "coef")
    out = {"type": genre, "heure": _heure(quand)}
    if coef is not None:
        try:
            out["coefficient"] = int(float(coef))
        except (TypeError, ValueError):
            out["coefficient"] = coef
    hauteur = prends("height", "hauteur", "value")
    if hauteur is not None:
        try:
            out["hauteur_m"] = round(float(hauteur), 2)
        except (TypeError, ValueError):
            pass
    return out


@mcp.tool()
async def marees(lieu: str, jour: str | None = None, jours: int = 1) -> dict:
    """Heures des pleines et basses mers, et coefficient, pour un lieu du littoral français.

    Répond à « l'estran est-il découvert à 15 h ? » et « ça va tirer fort samedi
    dans le golfe ? ». Rien d'autre : pas de courbe de hauteurs, pas de calcul de
    marnage, pas de planification nautique.

    lieu : « lat,lng », un nom de port (« Auray », « Port-Navalo ») ou un lieu en
           toutes lettres — rattaché au port le plus proche, dont la DISTANCE est
           toujours rendue.
    jour : « AAAA-MM-JJ » (défaut aujourd'hui). jours : 1 à 15.

    ⚠️ Le COEFFICIENT n'est pas local : il est calculé pour Brest et vaut pour
    toute la Manche et l'Atlantique. Il ne veut rien dire en Méditerranée.
    ⚠️ Il est donné par une source qui le dit elle-même NON OFFICIEL (le Shom
    fait autorité et vend son service) : bon pour décider une sortie, pas pour
    un document qui engage.
    """
    if err := _need_key():
        return {"error": err}
    bornes = _bornes(jour, jours)
    if isinstance(bornes, str):
        return {"error": bornes}
    debut, fin = bornes

    async with _client() as client:
        res = await _resout(client, lieu)
        if isinstance(res, str):
            return {"error": res}
        lat, lng, libelle = res
        sites = await _sites(client)
        if isinstance(sites, str):
            return {"error": sites}
        port, distance = _proche(sites, lat, lng)
        if distance > TROP_LOIN_KM:
            return {"error": f"aucun port de référence près de « {libelle} » : le plus proche "
                             f"est {port['site_name']}, à {round(distance)} km. Le registre "
                             f"couvre la Manche, l'Atlantique et la mer du Nord — la "
                             f"Méditerranée n'y figure pas."}
        try:
            r = await client.get(f"{API}/tide-extrema", params={
                "site": port["site_id"], "from": debut, "to": fin,
                "tz": os.environ.get("TZ", "Europe/Paris"), "key": _key(),
            })
            data = r.json()
        except httpx.HTTPError as exc:
            return {"error": f"api-maree.fr injoignable ({type(exc).__name__})."}
        except ValueError:
            return {"error": f"réponse illisible d'api-maree.fr : {(r.text or '')[:200]}"}

    # Formes d'erreur relevées sur le service vivant le 2026-08-06 : une clé
    # absente sort en 422 avec un `detail` de validation FastAPI, une clé fausse
    # en 401 `{"error": "invalid_api_key"}`. Les deux se disent en clair — un
    # « HTTP 401 » sec enverrait chercher le problème côté réseau.
    if r.status_code == 401:
        return {"error": "clé api-maree.fr refusée (invalid_api_key) : vérifier API_MAREE_KEY "
                         "dans l'environnement du serveur."}
    if r.status_code == 422:
        return {"error": "requête refusée par api-maree.fr (paramètre manquant ou invalide) : "
                         f"{str(data)[:200]}"}
    if r.status_code == 429:
        return {"error": "quota api-maree.fr atteint (360 requêtes/heure) : réessayer plus tard."}
    if r.status_code != 200:
        detail = data.get("detail") if isinstance(data, dict) else None
        return {"error": f"api-maree.fr : HTTP {r.status_code}{f' — {detail}' if detail else ''}"}

    # Forme réelle, relevée sur le service le 2026-08-06 : `data` est une liste
    # de JOURS, chacun portant sa `date` et sa liste d'`extrema`. Les heures y
    # sont déjà en « HH:MM » local (pas d'ISO à découper), et le coefficient
    # s'appelle `coef`. Il ne porte que sur les pleines mers.
    par_jour: dict[str, list] = {}
    for j in data.get("data") or []:
        if not isinstance(j, dict):
            continue
        d = j.get("date") or debut
        for e in j.get("extrema") or []:
            if isinstance(e, dict):
                par_jour.setdefault(d, []).append(_range(e))

    # ⚠️ La donnée est en CC BY : l'attribution n'est pas une politesse, c'est la
    # condition de la licence. On repasse celle que la source fournit elle-même,
    # telle quelle, plutôt que d'en rédiger une approximation.
    attribution = (data.get("source") or {}).get("attribution")
    out = {
        "lieu": libelle,
        "port": port["site_name"],
        "distance_km": round(distance, 1),
        "source": "api-maree.fr (composantes harmoniques Ifremer/PREVIMER, CC BY)",
        "attribution": re.sub(r"<[^>]+>", "", attribution) if attribution else None,
        "coefficient_note": "coefficient NON OFFICIEL, calculé pour Brest — il vaut pour la "
                            "Manche et l'Atlantique, et n'a aucun sens en Méditerranée. "
                            "Le Shom fait autorité.",
    }
    # Un port lointain ne prédit pas votre estran : on le dit, on ne le devine pas.
    if distance > LOIN_KM:
        out["avertissement"] = (f"le port de référence est à {round(distance)} km : les heures "
                                f"décalent avec la distance, à vérifier si la minute compte.")
    if not par_jour:
        # Plutôt qu'un objet vide, on rend ce que l'amont a dit : un silence
        # serait indébogable, et la forme exacte n'a pas pu être observée ici.
        out["error"] = "aucune marée lue dans la réponse — forme inattendue."
        out["reponse_brute"] = str(data)[:600]
        return out
    out["jours"] = [{"date": d, "marees": v} for d, v in sorted(par_jour.items())]
    return out


@mcp.tool()
async def marees_ports(pres_de: str, max_results: int = 5) -> dict:
    """Les ports de référence connus autour d'un lieu, avec leur distance.

    Sert à choisir : dans le golfe du Morbihan, Auray et Port-Navalo ne donnent
    pas la même heure. 131 ports couverts, littoral français.

    pres_de : « lat,lng », un nom de port ou un lieu en toutes lettres.
    """
    async with _client() as client:
        res = await _resout(client, pres_de)
        if isinstance(res, str):
            return {"error": res}
        lat, lng, libelle = res
        sites = await _sites(client)
        if isinstance(sites, str):
            return {"error": sites}
    proches = sorted(sites, key=lambda s: _haversine(lat, lng, s["latitude"], s["longitude"]))
    n = max(1, min(int(max_results or 5), 20))
    return {
        "lieu": libelle,
        "ports": [{
            "nom": s["site_name"],
            "id": s["site_id"],
            "latlng": f"{s['latitude']},{s['longitude']}",
            "distance_km": round(_haversine(lat, lng, s["latitude"], s["longitude"]), 1),
        } for s in proches[:n]],
    }


if __name__ == "__main__":
    # Local stdio debugging: `python -m rosetta.addons.marees`.
    mcp.run()
