"""marees addon: la fenêtre du modèle, le port lointain, et le coefficient qui
n'est pas local.

⚠️ La forme exacte de `/tide-extrema` n'a PAS pu être observée : la clé
appartient à l'utilisateur (compte gratuit api-maree.fr). Les tests jouent donc
la forme documentée, et vérifient surtout que l'addon ne rend jamais un silence
— si la charge ne correspond pas, elle doit remonter telle quelle.

Tout est mocké (httpx.MockTransport, aucun réseau).
"""

import asyncio
import json
from datetime import date, timedelta

import httpx
import pytest

from rosetta.addons import marees


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv("API_MAREE_KEY", "test-key")
    monkeypatch.setenv("TZ", "Europe/Paris")
    marees._sites_cache = None
    yield
    marees._transport = None
    marees._sites_cache = None


SITES = {"sites": [
    {"site_id": "auray-st-goustan", "site_name": "Auray (St-Goustan)",
     "latitude": 47.6667, "longitude": -2.9833},
    {"site_id": "port-navalo", "site_name": "Port-Navalo",
     "latitude": 47.5417, "longitude": -2.9167},
    {"site_id": "brest", "site_name": "Brest", "latitude": 48.3833, "longitude": -4.4833},
]}

EXTREMA = {"extrema": [
    {"type": "PM", "time": "2026-08-07T06:12:00", "height": 4.9, "coefficient": 78},
    {"type": "BM", "time": "2026-08-07T12:31:00", "height": 1.2},
    {"type": "PM", "time": "2026-08-07T18:38:00", "height": 5.1, "coefficient": 81},
]}


def serve(routes, capture=None):
    """routes: (fragment d'URL -> Response). La première correspondance gagne."""
    def handler(request):
        if capture is not None:
            capture.append(request)
        for frag, resp in routes:
            if frag in str(request.url):
                return resp
        return httpx.Response(404, json={"detail": "non routé"})
    marees._transport = httpx.MockTransport(handler)


NOMINAL = [("/sites", httpx.Response(200, json=SITES)),
           ("/tide-extrema", httpx.Response(200, json=EXTREMA))]


# --- la clé ---------------------------------------------------------------

def test_sans_cle_le_dit_sans_appeler(monkeypatch):
    monkeypatch.delenv("API_MAREE_KEY", raising=False)
    out = run(marees.marees("47.65,-2.75"))
    assert "API_MAREE_KEY" in out["error"]


# --- la fenêtre du modèle -------------------------------------------------

def test_hors_fenetre_refuse_au_lieu_d_extrapoler():
    """Le modèle amont s'arrête à J±30. Déborder en silence inventerait une marée."""
    loin = (date.today() + timedelta(days=45)).isoformat()
    out = run(marees.marees("47.65,-2.75", jour=loin))
    assert "hors de portée" in out["error"]
    vieux = (date.today() - timedelta(days=60)).isoformat()
    assert "hors de portée" in run(marees.marees("47.65,-2.75", jour=vieux))["error"]


def test_date_mal_formee_refusee():
    assert "AAAA-MM-JJ" in run(marees.marees("47.65,-2.75", jour="7 août"))["error"]


def test_la_fenetre_demandee_part_bien_a_l_amont():
    seen = []
    serve(NOMINAL, capture=seen)
    jour = date.today().isoformat()
    run(marees.marees("47.65,-2.75", jour=jour, jours=3))
    req = [r for r in seen if "tide-extrema" in str(r.url)][0]
    assert req.url.params["from"] == jour
    assert req.url.params["to"] == (date.today() + timedelta(days=2)).isoformat()
    assert req.url.params["key"] == "test-key"


# --- le rattachement au port ---------------------------------------------

def test_le_port_le_plus_proche_et_sa_distance():
    serve(NOMINAL)
    out = run(marees.marees("47.615,-2.918"))          # Baden
    assert out["port"] == "Auray (St-Goustan)"
    assert 0 < out["distance_km"] < 15
    assert "avertissement" not in out


def test_un_port_lointain_est_signale():
    """Un port à 50 km ne prédit pas votre estran — et le silence serait pire."""
    serve(NOMINAL)
    out = run(marees.marees("46.20,-1.55"))            # La Rochelle, loin des 3 ports mockés
    assert out["distance_km"] > marees.LOIN_KM
    assert "km" in out["avertissement"]


def test_un_nom_de_port_evite_le_geocodage():
    seen = []
    serve(NOMINAL, capture=seen)
    out = run(marees.marees("Port-Navalo"))
    assert out["port"] == "Port-Navalo"
    assert not any("geopf" in str(r.url) for r in seen)


def test_un_lieu_en_toutes_lettres_passe_par_l_IGN():
    serve([("geopf", httpx.Response(200, json={"features": [
        {"geometry": {"coordinates": [-2.918, 47.615]}, "properties": {"label": "Baden"}}]})),
        *NOMINAL])
    out = run(marees.marees("Baden"))
    assert out["lieu"] == "Baden" and out["port"] == "Auray (St-Goustan)"


def test_lieu_introuvable():
    serve([("geopf", httpx.Response(200, json={"features": []})), *NOMINAL])
    assert "introuvable" in run(marees.marees("Nawakville"))["error"]


# --- la lecture des marées -----------------------------------------------

def test_heures_et_coefficient():
    serve(NOMINAL)
    out = run(marees.marees("47.615,-2.918", jour="2026-08-07"))
    jour = out["jours"][0]
    assert jour["date"] == "2026-08-07"
    assert [m["type"] for m in jour["marees"]] == ["pleine mer", "basse mer", "pleine mer"]
    assert [m["heure"] for m in jour["marees"]] == ["06:12", "12:31", "18:38"]
    # Le coefficient est porté par les pleines mers, et par elles seules.
    assert jour["marees"][0]["coefficient"] == 78
    assert "coefficient" not in jour["marees"][1]


def test_le_coefficient_est_toujours_etiquete_non_officiel():
    """Ce n'est pas un ornement : c'est ce qui sépare une suggestion d'une affirmation."""
    serve(NOMINAL)
    out = run(marees.marees("47.615,-2.918"))
    assert "NON OFFICIEL" in out["coefficient_note"]
    assert "Brest" in out["coefficient_note"]
    assert "Méditerranée" in out["coefficient_note"]


def test_plusieurs_jours_sont_groupes():
    serve([("/sites", httpx.Response(200, json=SITES)),
           ("/tide-extrema", httpx.Response(200, json={"extrema": EXTREMA["extrema"] + [
               {"type": "PM", "time": "2026-08-08T07:02:00", "coefficient": 84}]}))])
    out = run(marees.marees("47.615,-2.918", jour="2026-08-07", jours=2))
    assert [j["date"] for j in out["jours"]] == ["2026-08-07", "2026-08-08"]


def test_une_forme_inattendue_remonte_la_charge_brute():
    """Un objet vide serait indébogable — la forme exacte n'a pas pu être observée."""
    serve([("/sites", httpx.Response(200, json=SITES)),
           ("/tide-extrema", httpx.Response(200, json={"resultats": [{"quoi": "?"}]}))])
    out = run(marees.marees("47.615,-2.918"))
    assert "forme inattendue" in out["error"]
    assert "resultats" in out["reponse_brute"]


def test_quota_atteint_est_une_information():
    serve([("/sites", httpx.Response(200, json=SITES)),
           ("/tide-extrema", httpx.Response(429, json={"detail": "rate limited"}))])
    assert "quota" in run(marees.marees("47.615,-2.918"))["error"]


# --- les ports -----------------------------------------------------------

def test_liste_des_ports_triee_par_distance():
    serve(NOMINAL)
    out = run(marees.marees_ports("47.615,-2.918", max_results=2))
    assert [p["nom"] for p in out["ports"]] == ["Auray (St-Goustan)", "Port-Navalo"]
    assert out["ports"][0]["distance_km"] < out["ports"][1]["distance_km"]


def test_la_liste_des_ports_n_est_tiree_qu_une_fois():
    """Elle est statique : la retirer à chaque question mangerait le quota."""
    seen = []
    serve(NOMINAL, capture=seen)
    run(marees.marees_ports("47.615,-2.918"))
    run(marees.marees_ports("47.5,-2.9"))
    assert len([r for r in seen if str(r.url).endswith("/sites")]) == 1
