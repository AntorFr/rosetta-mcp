"""trace addon: the lon/lat flip, BRouter's two non-JSON failures, the ascent
filter, and the promise that geometry never reaches the caller's transcript.

Numbers here reproduce what the live services returned on 2026-08-04 (see the
module docstring): BRouter answers 400 with plain text and 500 with an empty
body, and IGN rejects real JSON booleans.

All against mocked upstreams (httpx.MockTransport, no network).
"""

import asyncio
import json

import httpx
import pytest
from starlette.requests import Request

from rosetta.addons import trace


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("BROUTER_URL", raising=False)
    monkeypatch.delenv("OVERPASS_URL", raising=False)
    monkeypatch.delenv("IGN_ALTI_URL", raising=False)
    monkeypatch.setenv("ROSETTA_EXTERNAL_URL", "https://rosetta.test")
    yield
    trace._transport = None


def geojson(coords, length="1000", ascend="12", time="900", messages=None):
    """A BRouter feature. `coords` are (lng, lat, ele) triples, its own order."""
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {
                "creator": "BRouter-1.7.9",
                "track-length": length,
                "filtered ascend": ascend,
                "plain-ascend": "0",
                "total-time": time,
                "messages": messages or [],
            },
            "geometry": {"type": "LineString", "coordinates": [list(c) for c in coords]},
        }],
    }


def serve(response, capture=None):
    """Install a MockTransport answering everything with `response`, recording
    each request into `capture`."""
    def handler(request):
        if capture is not None:
            capture.append(request)
        return response(request) if callable(response) else response
    trace._transport = httpx.MockTransport(handler)


FLAT = [(-2.7592, 47.6535, 4.0), (-2.7585, 47.6540, 4.0), (-2.7580, 47.6546, 4.0)]


# --- the flip -------------------------------------------------------------

def test_brouter_recoit_lon_lat_pas_lat_lng():
    """The one bug that produces a plausible route instead of an error."""
    seen = []
    serve(httpx.Response(200, json=geojson(FLAT)), capture=seen)
    run(trace.trace_calcule("47.65356,-2.75921; 47.6546605,-2.758024"))
    lonlats = seen[0].url.params["lonlats"]
    assert lonlats == "-2.75921,47.65356|-2.758024,47.6546605"


def test_profil_francais_traduit_vers_brouter():
    seen = []
    serve(httpx.Response(200, json=geojson(FLAT)), capture=seen)
    run(trace.trace_calcule("47.1,-2.1; 47.2,-2.2", profil="rando"))
    assert seen[0].url.params["profile"] == "hiking-beta"
    run(trace.trace_calcule("47.1,-2.1; 47.2,-2.2", profil="velo"))
    assert seen[1].url.params["profile"] == "trekking"


def test_profil_inconnu_passe_tel_quel_au_routeur():
    """An unmapped name is forwarded rather than rejected here: BRouter ships
    profiles this table does not know, and it is the one that decides."""
    seen = []
    serve(httpx.Response(200, json=geojson(FLAT)), capture=seen)
    run(trace.trace_calcule("47.1,-2.1; 47.2,-2.2", profil="vm-forum-liegerad-schnell"))
    assert seen[0].url.params["profile"] == "vm-forum-liegerad-schnell"


# --- BRouter's two non-JSON failures --------------------------------------

def test_profil_inconnu_500_corps_vide():
    serve(httpx.Response(500, content=b""))
    out = run(trace.trace_calcule("47.1,-2.1; 47.2,-2.2", profil="nawak"))
    assert "inconnu du routeur" in out["error"]


def test_ilot_400_texte_brut_remonte_le_message():
    serve(httpx.Response(400, text="target island detected for section 0"))
    out = run(trace.trace_calcule("47.1,-2.1; 47.2,-2.2"))
    assert "target island detected" in out["error"]


def test_reponse_illisible_ne_leve_pas():
    serve(httpx.Response(200, text="<html>maintenance</html>"))
    out = run(trace.trace_calcule("47.1,-2.1; 47.2,-2.2"))
    assert "illisible" in out["error"]


# --- entrées ---------------------------------------------------------------

def test_un_seul_point_refuse():
    out = run(trace.trace_calcule("47.1,-2.1"))
    assert "au moins deux points" in out["error"]


def test_coordonnees_inversees_hors_bornes_refusees():
    out = run(trace.trace_calcule("-2.1,247.1; 47.2,-2.2"))
    assert "sort des bornes" in out["error"]


def test_points_acceptes_en_liste():
    serve(httpx.Response(200, json=geojson(FLAT)))
    out = run(trace.trace_calcule(["47.1,-2.1", "47.2,-2.2"]))
    assert out["distance_m"] == 1000


# --- le dénivelé -----------------------------------------------------------

def test_hysteresis_filtre_le_bruit():
    """A 4 m ripple repeated is terrain noise, not a climb."""
    serie = [100, 103, 100, 103, 100, 103, 100]
    assert trace._denivele(serie) == (0, 0)


def test_hysteresis_symetrique_sur_une_boucle():
    serie = [100, 120, 140, 120, 100]
    up, down = trace._denivele(serie)
    assert up == down


def test_denivele_brut_serait_bien_plus_gros():
    """Documented so the filter is never quietly dropped: raw accumulation
    inflated a real 9.5 km loop from 378 m to 506 m."""
    serie = [100, 103, 100, 103, 100]
    assert trace._denivele(serie, threshold=0) == (6, 6)
    assert trace._denivele(serie) == (0, 0)


# --- la géométrie ne sort pas ---------------------------------------------

def test_trace_calcule_ne_rend_jamais_la_geometrie():
    """The whole point of the addon: numbers and a URL, never the track."""
    serve(httpx.Response(200, json=geojson(FLAT)))
    out = run(trace.trace_calcule("47.1,-2.1; 47.2,-2.2"))
    blob = json.dumps(out)
    assert "geometrie" not in out and "altitudes" not in out
    assert "47.65" not in blob.replace(out["geometrie_url"], "")
    assert out["geometrie_url"].startswith("https://rosetta.test/trace/geometrie?")


def test_url_de_geometrie_rejoue_les_memes_parametres():
    serve(httpx.Response(200, json=geojson(FLAT)))
    out = run(trace.trace_calcule("47.1,-2.1; 47.2,-2.2", profil="rando", altimetrie="ign"))
    assert "profil=rando" in out["geometrie_url"]
    assert "altimetrie=ign" in out["geometrie_url"]


# --- l'encodage ------------------------------------------------------------

def decode(encoded, factor=1e5, dims=2):
    """Reference decoder, written from the algorithm rather than from the
    encoder above - a shared bug would otherwise pass unnoticed."""
    out, i, acc = [], 0, [0] * dims
    while i < len(encoded):
        for d in range(dims):
            shift, result = 0, 0
            while True:
                b = ord(encoded[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            acc[d] += ~(result >> 1) if result & 1 else (result >> 1)
        out.append(tuple(v / factor for v in acc))
    return out


def test_polyline_aller_retour():
    coords = [(47.65356, -2.75921), (47.6546605, -2.758024), (47.6559693, -2.7570677)]
    got = decode(trace._encode_path(coords))
    assert len(got) == 3
    for (a, b), (c, d) in zip(coords, got):
        assert abs(a - c) < 1e-5 and abs(b - d) < 1e-5


def test_altitudes_encodees_en_metres_entiers():
    got = [v[0] for v in decode(trace._encode_series([4.0, 4.2, 12.0, 3.0]), factor=1, dims=1)]
    assert got == [4, 4, 12, 3]


# --- revêtement, escaliers, écarts, étapes ---------------------------------

HEADER = ["Longitude", "Latitude", "Elevation", "Distance", "CostPerKm", "ElevCost",
          "TurnCost", "NodeCost", "InitialCost", "WayTags", "NodeTags", "Time", "Energy"]


def msg(distance, waytags):
    row = [""] * len(HEADER)
    row[HEADER.index("Distance")] = str(distance)
    row[HEADER.index("WayTags")] = waytags
    return row


def test_repartition_revetement_et_escaliers():
    messages = [HEADER,
                msg(300, "highway=footway surface=paving_stones"),
                msg(120, "highway=steps surface=asphalt"),
                msg(200, "highway=path")]
    surfaces, voies, steps = trace._repartition(messages)
    assert surfaces == {"pavés": 300, "inconnu": 200, "asphalte": 120}
    assert voies["escaliers"] == 120 and voies["sentier"] == 200
    assert steps == 120


def test_surface_inconnue_passe_le_jeton_brut():
    surfaces, _, _ = trace._repartition([HEADER, msg(50, "highway=path surface=woodchips")])
    assert "woodchips" in surfaces


def test_ecart_du_repere_a_la_trace_est_mesure():
    """Alfred documented a 27 m gap by hand on the Vannes loop; it is computed now."""
    serve(httpx.Response(200, json=geojson(FLAT)))
    # Second point pushed ~30 m north of the track.
    out = run(trace.trace_calcule("47.6535,-2.7592; 47.65427,-2.7585"))
    assert out["ecart_reperes_m"][0] == 0
    assert 20 <= out["ecart_reperes_m"][1] <= 40


def test_etapes_entre_reperes_consecutifs():
    serve(httpx.Response(200, json=geojson(FLAT)))
    out = run(trace.trace_calcule("47.6535,-2.7592; 47.6540,-2.7585; 47.6546,-2.7580"))
    assert [e["de"] for e in out["etapes"]] == [1, 2]
    assert all(e["distance_m"] > 0 for e in out["etapes"])


def test_boucle_la_derniere_etape_ne_repart_pas_a_l_envers():
    """Measured live on the Vannes loop: a global nearest-vertex search snapped
    the closing point back to index 0 and billed the last 260 m as 2 770 m."""
    loop = FLAT + [(-2.7585, 47.6540, 4.0), (-2.7592, 47.6535, 4.0)]
    serve(httpx.Response(200, json=geojson(loop)))
    out = run(trace.trace_calcule("47.6535,-2.7592; 47.6546,-2.7580; 47.6535,-2.7592"))
    aller, retour = out["etapes"][0]["distance_m"], out["etapes"][1]["distance_m"]
    assert aller == pytest.approx(retour, rel=0.05)
    assert out["ecart_reperes_m"][-1] == 0


# --- IGN -------------------------------------------------------------------

def test_ign_recoit_des_booleens_en_CHAINE():
    """A real JSON boolean is rejected with BAD_PARAMETER."""
    seen = []

    def handler(request):
        seen.append(request)
        if "geopf" in str(request.url):
            return httpx.Response(200, json={"elevations": [
                {"z": 10.0}, {"z": 30.0}, {"z": 10.0}]})
        return httpx.Response(200, json=geojson(FLAT))

    serve(handler)
    out = run(trace.trace_calcule("47.1,-2.1; 47.2,-2.2", altimetrie="ign"))
    body = json.loads(seen[1].content)
    assert body["indent"] == "false" and body["measures"] == "false"
    assert isinstance(body["indent"], str)
    assert out["altimetrie"] == "IGN RGE ALTI"
    assert out["denivele_pos_m"] == 20


def test_ign_profil_incomplet_retombe_sur_le_routeur():
    def handler(request):
        if "geopf" in str(request.url):
            return httpx.Response(200, json={"elevations": [{"z": 10.0}]})
        return httpx.Response(200, json=geojson(FLAT))

    serve(handler)
    out = run(trace.trace_calcule("47.1,-2.1; 47.2,-2.2", altimetrie="ign"))
    assert out["altimetrie"].startswith("routeur")
    assert "incomplet" in out["avertissement"]


def test_ign_hors_couverture_signale():
    def handler(request):
        if "geopf" in str(request.url):
            return httpx.Response(200, json={"elevations": [
                {"z": -99999.0}, {"z": 10.0}, {"z": 12.0}]})
        return httpx.Response(200, json=geojson(FLAT))

    serve(handler)
    out = run(trace.trace_calcule("47.1,-2.1; 47.2,-2.2", altimetrie="ign"))
    assert "hors couverture" in out["avertissement"]


# --- POI -------------------------------------------------------------------

def test_pois_type_inconnu_liste_les_types_valides():
    out = run(trace.trace_pois("47.1,-2.1", types="licorne"))
    assert "licorne" in out["error"] and "eau" in out["error"]


def test_pois_rend_les_tags_tiers_etiquetes():
    serve(httpx.Response(200, json={"elements": [{
        "type": "node", "id": 42, "lat": 47.101, "lon": -2.101,
        "tags": {"amenity": "drinking_water", "name": "Fontaine du bourg",
                 "website": "https://exemple.fr", "surveillance": "no"},
    }]}))
    out = run(trace.trace_pois("47.1,-2.1", types="eau"))
    poi = out["resultats"]["eau"][0]
    assert poi["genre"] == "amenity=drinking_water"
    assert poi["tags_osm"]["name"] == "Fontaine du bourg"
    assert "surveillance" not in poi["tags_osm"]
    assert poi["osm"] == "node/42"
    assert "non fiable" in out["source"] or "jamais à suivre" in out["source"]


def test_pois_relation_utilise_le_centre():
    serve(httpx.Response(200, json={"elements": [{
        "type": "relation", "id": 7, "center": {"lat": 47.11, "lon": -2.11},
        "tags": {"route": "hiking", "name": "GR 34"},
    }]}))
    out = run(trace.trace_pois("47.1,-2.1", types="sentier"))
    assert out["resultats"]["sentier"][0]["latlng"] == "47.11,-2.11"
    assert out["resultats"]["sentier"][0]["distance_m"] > 0


def test_pois_le_mobilier_urbain_n_ecrase_pas_les_points_de_vue():
    """Live around Vannes (2026-08-04): a pooled limit returned eight benches
    and neither of the two types actually wanted."""
    bancs = [{"type": "node", "id": i, "lat": 47.1 + i / 10000, "lon": -2.1,
              "tags": {"amenity": "bench"}} for i in range(40)]
    vue = [{"type": "node", "id": 999, "lat": 47.15, "lon": -2.15,
            "tags": {"tourism": "viewpoint", "name": "Panorama"}}]
    serve(httpx.Response(200, json={"elements": bancs + vue}))
    out = run(trace.trace_pois("47.1,-2.1", types="banc,vue", max_results=3))
    assert len(out["resultats"]["banc"]) == 3
    assert out["resultats"]["vue"][0]["tags_osm"]["name"] == "Panorama"


def test_pois_type_sans_resultat_est_dit_pas_tu():
    serve(httpx.Response(200, json={"elements": []}))
    out = run(trace.trace_pois("47.1,-2.1", types="eau,vue"))
    assert out["resultats"] == {}
    assert set(out["vides"]) == {"eau", "vue"}


def test_pois_sans_tag_utile_ne_porte_pas_de_dict_vide():
    serve(httpx.Response(200, json={"elements": [{
        "type": "node", "id": 1, "lat": 47.101, "lon": -2.101,
        "tags": {"amenity": "bench"}}]}))
    out = run(trace.trace_pois("47.1,-2.1", types="banc"))
    assert "tags_osm" not in out["resultats"]["banc"][0]


def test_pois_quota_sature_est_une_information():
    serve(httpx.Response(429, text="rate limited"))
    out = run(trace.trace_pois("47.1,-2.1", types="eau"))
    assert "saturé" in out["error"]


def test_pois_rayon_plafonne():
    seen = []
    serve(httpx.Response(200, json={"elements": []}), capture=seen)
    run(trace.trace_pois("47.1,-2.1", types="eau", rayon_m=99999))
    assert "around:5000" in seen[0].content.decode()


# --- la route HTTP ---------------------------------------------------------

def request(query: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/trace/geometrie",
                    "query_string": query.encode(), "headers": []})


def test_route_geometrie_rend_la_trace_encodee():
    serve(httpx.Response(200, json=geojson(FLAT)))
    resp = run(trace.geometrie(request("points=47.1,-2.1; 47.2,-2.2&profil=pieton")))
    body = json.loads(resp.body)
    assert resp.status_code == 200
    assert decode(body["geometrie"])[0] == pytest.approx((47.6535, -2.7592), abs=1e-4)
    assert body["points_trace"] == 3
    assert body["distance_m"] == 1000


def test_route_geometrie_refuse_des_points_illisibles():
    resp = run(trace.geometrie(request("points=nawak")))
    assert resp.status_code == 400


def test_route_geometrie_remonte_502_quand_le_routeur_tombe():
    serve(httpx.Response(400, text="target island detected for section 0"))
    resp = run(trace.geometrie(request("points=47.1,-2.1; 47.2,-2.2")))
    assert resp.status_code == 502
    assert b"island" in resp.body
