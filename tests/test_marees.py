"""marees addon: la fenêtre du modèle, le port lointain, et le coefficient qui
n'est pas local.

La charge de `/tide-extrema` reproduite ici est la VRAIE, relevée sur le service
le 2026-08-06 (Auray, 7-8 août 2026). Elle a démenti la forme supposée : `data`
est une liste de JOURS portant chacun ses `extrema`, les heures sont déjà en
« HH:MM » locales, et le coefficient s'appelle `coef`. D'où ces fixtures : un
test écrit contre une forme devinée ne prouve rien.

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

# La vraie réponse du service, Auray, 7-8 août 2026 (verbatim, `source` élaguée).
EXTREMA = {
    "site_id": "auray-st-goustan", "site_name": "Auray (St-Goustan)",
    "timezone": "Europe/Paris", "unit": "m",
    "source": {"attribution": "Données de marée fournies par <a href=\"https://api-maree.fr/\">"
                              "api-maree.fr</a> sous licence CC BY."},
    "data": [
        {"date": "2026-08-07", "extrema": [
            {"type": "BM", "time": "06:12", "height": 1.681},
            {"type": "PM", "time": "12:34", "height": 3.846, "coef": 47},
            {"type": "BM", "time": "18:47", "height": 1.757}]},
        {"date": "2026-08-08", "extrema": [
            {"type": "PM", "time": "01:28", "height": 3.883, "coef": 45},
            {"type": "BM", "time": "07:25", "height": 1.752}]},
    ],
}


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


def test_un_port_un_peu_loin_est_signale():
    """Entre 25 et 100 km, on répond mais on le dit : les heures décalent."""
    serve(NOMINAL)
    out = run(marees.marees("47.30,-2.30"))            # ~50 km d'Auray
    assert marees.LOIN_KM < out["distance_km"] < marees.TROP_LOIN_KM
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
    assert [m["type"] for m in jour["marees"]] == ["basse mer", "pleine mer", "basse mer"]
    assert [m["heure"] for m in jour["marees"]] == ["06:12", "12:34", "18:47"]
    # ⚠️ Le coefficient s'appelle `coef` en amont, et ne porte QUE les pleines mers.
    assert jour["marees"][1]["coefficient"] == 47
    assert "coefficient" not in jour["marees"][0]


def test_attribution_CC_BY_reprise_et_denudee_de_son_HTML():
    """La donnée est en CC BY : l'attribution est la CONDITION de la licence,
    pas une politesse. On repasse celle de la source, sans ses balises."""
    serve(NOMINAL)
    out = run(marees.marees("47.615,-2.918"))
    assert "api-maree.fr" in out["attribution"] and "CC BY" in out["attribution"]
    assert "<a href" not in out["attribution"]


def test_une_autre_mer_est_REFUSEE_pas_avertie():
    """Sans ce refus, « Sète » rendait les marées de Bordeaux, à 389 km et sur
    une autre mer, sous un simple avertissement « ordre de grandeur ». Le
    registre ne contient aucun port méditerranéen (vérifié le 2026-08-06)."""
    serve(NOMINAL)
    out = run(marees.marees("43.40,3.69"))            # Sète
    assert "aucun port de référence" in out["error"]
    assert "Méditerranée" in out["error"]
    assert "jours" not in out


def test_le_coefficient_est_toujours_etiquete_non_officiel():
    """Ce n'est pas un ornement : c'est ce qui sépare une suggestion d'une affirmation."""
    serve(NOMINAL)
    out = run(marees.marees("47.615,-2.918"))
    assert "NON OFFICIEL" in out["coefficient_note"]
    assert "Brest" in out["coefficient_note"]
    assert "Méditerranée" in out["coefficient_note"]


def test_plusieurs_jours_sont_groupes():
    serve(NOMINAL)
    out = run(marees.marees("47.615,-2.918", jour="2026-08-07", jours=2))
    assert [j["date"] for j in out["jours"]] == ["2026-08-07", "2026-08-08"]
    assert out["jours"][1]["marees"][0]["coefficient"] == 45


def test_une_forme_inattendue_remonte_la_charge_brute():
    """Un objet vide serait indébogable — la forme exacte n'a pas pu être observée."""
    serve([("/sites", httpx.Response(200, json=SITES)),
           ("/tide-extrema", httpx.Response(200, json={"resultats": [{"quoi": "?"}]}))])
    out = run(marees.marees("47.615,-2.918"))
    assert "forme inattendue" in out["error"]
    assert "resultats" in out["reponse_brute"]


def test_cle_refusee_le_dit_en_clair():
    """Relevé sur le service vivant : 401 + {"error": "invalid_api_key"}.
    Un « HTTP 401 » sec enverrait chercher le problème côté réseau."""
    serve([("/sites", httpx.Response(200, json=SITES)),
           ("/tide-extrema", httpx.Response(401, json={"error": "invalid_api_key"}))])
    out = run(marees.marees("47.615,-2.918"))
    assert "invalid_api_key" in out["error"] and "API_MAREE_KEY" in out["error"]


def test_parametre_invalide_remonte_le_detail():
    serve([("/sites", httpx.Response(200, json=SITES)),
           ("/tide-extrema", httpx.Response(422, json={"detail": [
               {"type": "missing", "loc": ["query", "key"], "msg": "Field required"}]}))])
    assert "Field required" in run(marees.marees("47.615,-2.918"))["error"]


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
