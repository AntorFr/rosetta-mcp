"""transit addon : retard calculé, authentification par fournisseur, et les
quatre outils publics, sans réseau.

Le transport est injecté en remplaçant `transit.httpx.AsyncClient` par un
sous-type mince qui force un `httpx.MockTransport` ; rien dans `src/` n'a de
crochet pour cela. Les clés sont posées par `monkeypatch`, jamais réelles.
"""

import asyncio

import httpx
import pytest

from rosetta.addons import transit

_RealClient = httpx.AsyncClient

SNCF_KEY = "cle-sncf"
IDFM_KEY = "cle-idfm"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def cles(monkeypatch):
    monkeypatch.setenv("SNCF_API_KEY", SNCF_KEY)
    monkeypatch.setenv("IDFM_API_KEY", IDFM_KEY)


@pytest.fixture
def reseau(monkeypatch):
    """Installe un handler ; renvoie la liste des requêtes reçues."""
    vues: list[httpx.Request] = []

    def installer(handler):
        def enregistre(request):
            vues.append(request)
            return handler(request)

        class Client(_RealClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(enregistre)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(transit.httpx, "AsyncClient", Client)
        return vues

    return installer


def _gare(id_="stop_area:A", nom="Nantes"):
    return {"places": [{"name": "brut", "stop_area": {"id": id_, "name": nom}}]}


def _depart(theo, real, label="TER", direction="Rennes", network="SNCF"):
    return {
        "display_informations": {"label": label, "direction": direction, "network": network},
        "stop_date_time": {"base_departure_date_time": theo, "departure_date_time": real},
    }


def _routeur(departures=None, disruptions=None, dep_status=200, dis_status=200):
    def handler(request):
        chemin = request.url.path
        if chemin.endswith("/places"):
            return httpx.Response(200, json=_gare())
        if chemin.endswith("/departures"):
            return httpx.Response(dep_status, json=departures)
        if chemin.endswith("/disruptions"):
            return httpx.Response(dis_status, json=disruptions)
        return httpx.Response(404, json={})
    return handler


# --- _delay_minutes -------------------------------------------------------

def test_retard_egal_est_zero():
    assert transit._delay_minutes("20260807T120000", "20260807T120000") == 0


def test_retard_positif_quand_en_retard():
    assert transit._delay_minutes("20260807T120000", "20260807T120700") == 7


def test_retard_negatif_quand_en_avance():
    assert transit._delay_minutes("20260807T120500", "20260807T120000") == -5


def test_retard_absent():
    assert transit._delay_minutes(None, "20260807T120000") is None
    assert transit._delay_minutes("20260807T120000", None) is None
    assert transit._delay_minutes("", "") is None


def test_retard_mal_forme():
    assert transit._delay_minutes("pas une date", "20260807T120000") is None


# --- authentification -----------------------------------------------------

def test_sncf_basic_cle_en_login():
    assert transit.SNCF.request_kwargs() == {"auth": (SNCF_KEY, "")}


def test_idfm_en_tete_apikey():
    assert transit.IDFM.request_kwargs() == {"headers": {"apikey": IDFM_KEY}}


def test_cle_lue_a_chaque_appel(monkeypatch):
    monkeypatch.setenv("SNCF_API_KEY", "autre")
    assert transit.SNCF.request_kwargs() == {"auth": ("autre", "")}


def test_cle_absente_message_par_fournisseur(monkeypatch):
    monkeypatch.delenv("SNCF_API_KEY")
    monkeypatch.delenv("IDFM_API_KEY")
    sncf = run(transit.train_departures("Nantes"))
    idfm = run(transit.metro_departures("Nation"))
    assert "SNCF_API_KEY absente" in sncf["error"]
    assert "IDFM_API_KEY absente" in idfm["error"]
    assert "SNCF_API_KEY absente" in run(transit.train_disruptions())["error"]
    assert "IDFM_API_KEY absente" in run(transit.metro_disruptions("Nation"))["error"]


def test_cle_absente_aucun_appel_reseau(monkeypatch, reseau):
    monkeypatch.delenv("SNCF_API_KEY")
    vues = reseau(_routeur({}, {}))
    run(transit.train_departures("Nantes"))
    assert vues == []


def test_requetes_authentifiees_par_fournisseur(reseau):
    vues = reseau(_routeur({"departures": []}, {"disruptions": []}))
    run(transit.train_departures("Nantes"))
    assert vues, "aucune requête émise"
    assert all(v.headers["authorization"].startswith("Basic ") for v in vues)
    assert all("apikey" not in v.headers for v in vues)
    assert all(str(v.url).startswith(transit.SNCF.base_url) for v in vues)

    vues.clear()
    run(transit.metro_departures("Nation"))
    assert vues
    assert all(v.headers["apikey"] == IDFM_KEY for v in vues)
    assert all("authorization" not in v.headers for v in vues)
    assert all(str(v.url).startswith(transit.IDFM.base_url) for v in vues)


# --- résolution d'une gare ------------------------------------------------

def test_gare_http_en_erreur(reseau):
    reseau(lambda r: httpx.Response(500, json={}))
    out = run(transit.train_departures("Nantes"))
    assert out == {"error": "recherche impossible pour « Nantes » : HTTP 500"}


def test_gare_aucun_resultat(reseau):
    reseau(lambda r: httpx.Response(200, json={"places": []}))
    assert run(transit.metro_departures("Xyz")) == {"error": "aucun arrêt trouvé pour « Xyz »."}
    reseau(lambda r: httpx.Response(200, json={}))
    assert "aucun arrêt" in run(transit.metro_departures("Xyz"))["error"]


def test_gare_trouvee_interrogee_par_id(reseau):
    vues = reseau(_routeur({"departures": []}, {}))
    out = run(transit.train_departures("Nantes", count=3))
    assert out == {"station": "Nantes", "departures": []}
    places, departs = vues
    assert places.url.params["q"] == "Nantes"
    assert places.url.params["type[]"] == "stop_area"
    assert departs.url.path.endswith("/stop_areas/stop_area:A/departures")
    assert departs.url.params["count"] == "3"
    assert departs.url.params["data_freshness"] == "realtime"


def test_gare_sans_nom_de_stop_area_retombe_sur_le_nom_brut(reseau):
    def handler(request):
        if request.url.path.endswith("/places"):
            return httpx.Response(200, json={"places": [{"name": "Brut", "stop_area": {"id": "x"}}]})
        return httpx.Response(200, json={"departures": []})
    reseau(handler)
    assert run(transit.train_departures("B"))["station"] == "Brut"


# --- départs --------------------------------------------------------------

def test_departs_statuts(reseau):
    reseau(_routeur({"departures": [
        _depart("20260807T120000", "20260807T120400", label="A"),
        _depart("20260807T121000", "20260807T121000", label="B"),
        _depart("20260807T122000", None, label="C"),
    ]}))
    out = run(transit.train_departures("Nantes"))
    assert out["station"] == "Nantes"
    a, b, c = out["departures"]
    assert (a["line"], a["delay_min"], a["status"]) == ("A", 4, "retardé")
    assert (b["delay_min"], b["status"]) == (0, "à l'heure")
    assert (c["delay_min"], c["status"]) == (None, "inconnu")
    assert a["direction"] == "Rennes" and a["network"] == "SNCF"
    assert a["scheduled"] == "20260807T120000" and a["realtime"] == "20260807T120400"


def test_depart_en_avance_est_a_l_heure(reseau):
    reseau(_routeur({"departures": [_depart("20260807T120500", "20260807T120000")]}))
    d = run(transit.metro_departures("Nation"))["departures"][0]
    assert d["delay_min"] == -5
    assert d["status"] == "à l'heure"


def test_depart_ligne_retombe_sur_le_code(reseau):
    dep = {"display_informations": {"code": "B"}, "stop_date_time": {}}
    reseau(_routeur({"departures": [dep]}))
    d = run(transit.metro_departures("Nation"))["departures"][0]
    assert d["line"] == "B"
    assert d["status"] == "inconnu"


def test_departs_charge_d_erreur_avec_message(reseau):
    reseau(_routeur({"error": {"message": "quota dépassé"}}, dep_status=429))
    assert run(transit.train_departures("Nantes")) == {"error": "quota dépassé"}


def test_departs_charge_d_erreur_sans_message(reseau):
    reseau(_routeur({}, dep_status=503))
    assert run(transit.metro_departures("Nation")) == {"error": "HTTP 503"}


# --- perturbations --------------------------------------------------------

DISRUPTIONS = {"disruptions": [{
    "id": "d1",
    "messages": [{"text": "Grève"}],
    "severity": {"name": "bloquant", "effect": "NO_SERVICE"},
    "status": "active",
}, {
    "disruption_id": "d2",
    "severity": {},
    "status": "future",
}]}


def test_perturbations_sans_gare(reseau):
    vues = reseau(_routeur(disruptions=DISRUPTIONS))
    out = run(transit.train_disruptions())
    assert out["station"] is None
    assert [v.url.path for v in vues] == ["/v1/coverage/sncf/disruptions"]
    assert out["disruptions"][0] == {
        "title": "Grève", "severity": "bloquant", "effect": "NO_SERVICE", "status": "active",
    }
    # Sans message : le titre retombe sur l'identifiant.
    assert out["disruptions"][1]["title"] == "d2"
    assert out["disruptions"][1]["severity"] is None


def test_perturbations_avec_gare(reseau):
    vues = reseau(_routeur(disruptions=DISRUPTIONS))
    out = run(transit.metro_disruptions("Nation"))
    assert out["station"] == "Nantes"  # nom renvoyé par la résolution mockée
    assert len(out["disruptions"]) == 2
    assert vues[-1].url.path.endswith("/stop_areas/stop_area:A/disruptions")
    assert vues[-1].headers["apikey"] == IDFM_KEY


def test_perturbations_gare_introuvable(reseau):
    reseau(lambda r: httpx.Response(200, json={"places": []}))
    assert "aucun arrêt" in run(transit.train_disruptions("Xyz"))["error"]


def test_perturbations_charge_d_erreur(reseau):
    reseau(_routeur(disruptions={"error": {"message": "indisponible"}}, dis_status=500))
    assert run(transit.train_disruptions()) == {"error": "indisponible"}
    reseau(_routeur(disruptions={}, dis_status=502))
    assert run(transit.metro_disruptions()) == {"error": "HTTP 502"}
