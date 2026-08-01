"""meteo addon: the four Open-Meteo traps, the spot registry, and the wind.

The traps are the ones measured against the live API on 2026-08-01 (see the
module docstring): a model dropped out of a batched response, the key suffix
that depends on how many models survived, nulls past the horizon, and a
geocoder that puts "La Torche" in the Allier.

All against a mocked API (httpx.MockTransport, no network).
"""

import asyncio
import json

import httpx
import pytest

from rosetta.addons import meteo


def run(coro):
    return asyncio.run(coro)


def mock(handler):
    return httpx.MockTransport(handler)


TORCHE = json.dumps({"La Torche": {"latlng": "47.8367,-4.3492", "note": "cale nord"}})


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("ROSETTA_WIND_SPOTS", raising=False)
    monkeypatch.setenv("TZ", "Europe/Paris")


def hourly_body(speeds, gusts=None, dirs=None, day="2026-08-02", suffix=""):
    """An Open-Meteo forecast body, hour 0 upwards. `suffix` reproduces the
    multi-model key naming."""
    n = len(speeds)
    key = lambda name: f"{name}{suffix}"
    return {
        "hourly": {
            "time": [f"{day}T{h:02d}:00" for h in range(n)],
            key("wind_speed_10m"): speeds,
            key("wind_gusts_10m"): gusts if gusts is not None else [None] * n,
            key("wind_direction_10m"): dirs if dirs is not None else [None] * n,
        }
    }


def router(forecast=None, daily=None, geocode=None):
    """Dispatch on the URL, since a single call fans out to geocoding, daylight
    and one request per model."""
    def handler(request):
        url = str(request.url)
        if "geocoding-api" in url:
            return httpx.Response(200, json=geocode or {"results": []})
        if "daily=" in url or "daily" in request.url.params:
            return httpx.Response(200, json=daily or {
                "daily": {"sunrise": ["2026-08-02T06:55"], "sunset": ["2026-08-02T21:51"]}})
        body = forecast(request) if callable(forecast) else forecast
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body or hourly_body([10] * 24))
    return handler


# -- the spot registry ------------------------------------------------------

def test_registry_reads_both_shapes(monkeypatch):
    monkeypatch.setenv("ROSETTA_WIND_SPOTS", json.dumps({
        "La Torche": "47.8367,-4.3492",
        "Lac de Guerlédan": {"latlng": "48.20,-3.03", "note": "rive sud"},
    }))
    spots = meteo._spots()
    assert spots["La Torche"]["latlng"] == "47.8367,-4.3492"
    assert spots["Lac de Guerlédan"]["note"] == "rive sud"


def test_a_malformed_spot_is_skipped_not_fatal(monkeypatch):
    """One typo must not cost the other nine spots."""
    monkeypatch.setenv("ROSETTA_WIND_SPOTS", json.dumps({
        "bon": "47.8,-4.3", "cassé": "quelque part vers la mer",
    }))
    assert list(meteo._spots()) == ["bon"]


def test_invalid_json_disables_the_registry_quietly(monkeypatch):
    monkeypatch.setenv("ROSETTA_WIND_SPOTS", "{ceci n'est pas du json")
    assert meteo._spots() == {}


def test_spot_lookup_ignores_case_and_accents(monkeypatch):
    monkeypatch.setenv("ROSETTA_WIND_SPOTS", json.dumps({"Lac de Guerlédan": "48.20,-3.03"}))
    monkeypatch.setattr(meteo, "_transport", mock(
        lambda r: pytest.fail("registry hit must not reach the network")))

    async def go():
        async with meteo._client() as client:
            return await meteo._resolve(client, "  LAC DE GUERLEDAN ")

    place = run(go())
    assert place["resolution"] == "registre"
    assert (place["lat"], place["lng"]) == (48.20, -3.03)


def test_latlng_is_taken_literally(monkeypatch):
    monkeypatch.setattr(meteo, "_transport", mock(
        lambda r: pytest.fail("coordinates need no resolving")))

    async def go():
        async with meteo._client() as client:
            return await meteo._resolve(client, "47.8367,-4.3492")

    assert run(go())["resolution"] == "coordonnees"


# -- trap 4: the geocoder that drowns you -----------------------------------

def test_geocoded_spot_is_flagged_with_region_and_elevation(monkeypatch):
    """The live geocoder really does answer this for "La Torche" - a hamlet in
    the Allier, 400 km inland. The guess must be impossible to miss."""
    allier = {"results": [{
        "name": "La Torche", "latitude": 46.27842, "longitude": 2.7657,
        "elevation": 367.0, "admin1": "Rhône-Alpes", "admin2": "Allier", "country": "France",
    }]}
    monkeypatch.setattr(meteo, "_transport", mock(router(geocode=allier)))

    async def go():
        async with meteo._client() as client:
            return await meteo._resolve(client, "La Torche")

    place = run(go())
    assert place["resolution"] == "geocodage"
    assert "avertissement" in place
    assert place["altitude_m"] == 367.0
    assert "Allier" in place["lieu_resolu"]


def test_the_registry_beats_the_geocoder(monkeypatch):
    """Which is the whole point of having one."""
    monkeypatch.setenv("ROSETTA_WIND_SPOTS", TORCHE)
    monkeypatch.setattr(meteo, "_transport", mock(
        lambda r: pytest.fail("the registry must answer before any geocoding")))

    async def go():
        async with meteo._client() as client:
            return await meteo._resolve(client, "la torche")

    place = run(go())
    assert place["resolution"] == "registre"
    assert place["lat"] == 47.8367


# -- traps 1 and 2: one model, one request ----------------------------------

def test_each_model_gets_its_own_request(monkeypatch):
    """THE design decision. Batching is what lets a model vanish silently and
    what makes the key suffix ambiguous; one request per model removes both."""
    asked = []

    def handler(request):
        if "daily" in request.url.params:
            return httpx.Response(200, json={"daily": {
                "sunrise": ["2026-08-02T06:55"], "sunset": ["2026-08-02T21:51"]}})
        asked.append(request.url.params["models"])
        return httpx.Response(200, json=hourly_body([12] * 24))

    monkeypatch.setattr(meteo, "_transport", mock(handler))
    run(meteo.wind_forecast("47.8,-4.3", jour="2026-08-02",
                            modeles="arome_hd,arpege,ecmwf"))
    assert asked == ["meteofrance_arome_france_hd", "meteofrance_arpege_europe", "ecmwf_ifs025"]
    # One model per call means exactly one `models` value per request - never a
    # comma-separated batch.
    assert all("," not in a for a in asked)


def test_out_of_domain_model_is_reported_not_swallowed(monkeypatch):
    """Asked alone, Open-Meteo is honest: HTTP 400, "No data is available for
    this location". That answer must survive to the agent."""
    def handler(request):
        if "daily" in request.url.params:
            return httpx.Response(200, json={"daily": {
                "sunrise": ["2026-08-02T06:55"], "sunset": ["2026-08-02T21:51"]}})
        if "arome" in request.url.params["models"]:
            return httpx.Response(400, json={
                "error": True, "reason": "No data is available for this location"})
        return httpx.Response(200, json=hourly_body([12] * 24))

    monkeypatch.setattr(meteo, "_transport", mock(handler))
    got = run(meteo.wind_forecast("47.8,-4.3", jour="2026-08-02", modeles="arome_hd,ecmwf"))
    by_name = {m["modele"]: m for m in got["modeles"]}
    assert "No data is available" in by_name["arome_hd"]["erreur"]
    # And the model that DID answer is still attributed to itself, by name.
    assert by_name["ecmwf"]["resume"]["vent_moyen_kn"] == 12


def test_a_suffixed_key_is_still_read(monkeypatch):
    """Defence in depth: if a future change ever batches the call again, the
    suffixed column must be found rather than silently read as empty."""
    monkeypatch.setattr(meteo, "_transport", mock(router(
        forecast=hourly_body([14] * 24, suffix="_ecmwf_ifs025"))))
    got = run(meteo.wind_forecast("47.8,-4.3", jour="2026-08-02", modeles="ecmwf"))
    assert got["modeles"][0]["resume"]["vent_max_kn"] == 14


# -- trap 3: nulls past the horizon -----------------------------------------

def test_null_hours_are_dropped_never_read_as_calm(monkeypatch):
    """Past its horizon a model returns nulls. A null is "unknown", and calling
    it 0 knots would invent a windless afternoon."""
    speeds = [12] * 12 + [None] * 12
    monkeypatch.setattr(meteo, "_transport", mock(router(forecast=hourly_body(speeds))))
    got = run(meteo.wind_forecast("47.8,-4.3", jour="2026-08-02", de=0, a=23,
                                  modeles="arome_hd"))
    rows = got["modeles"][0]["heures"]
    assert len(rows) == 12
    assert all(r["vent_kn"] == 12 for r in rows)
    assert got["modeles"][0]["resume"]["vent_min_kn"] == 12


def test_a_window_entirely_past_the_horizon_says_so(monkeypatch):
    monkeypatch.setattr(meteo, "_transport", mock(router(forecast=hourly_body([None] * 24))))
    got = run(meteo.wind_forecast("47.8,-4.3", jour="2026-08-02", de=8, a=18,
                                  modeles="arome_hd"))
    assert "horizon" in got["modeles"][0]["erreur"]


# -- the wind itself --------------------------------------------------------

def test_rows_carry_gust_ratio_and_french_bearing(monkeypatch):
    monkeypatch.setattr(meteo, "_transport", mock(router(
        forecast=hourly_body([10] * 24, gusts=[16] * 24, dirs=[247] * 24))))
    got = run(meteo.wind_forecast("47.8,-4.3", jour="2026-08-02", de=12, a=12))
    row = got["modeles"][0]["heures"][0]
    assert row == {"h": "12:00", "vent_kn": 10, "rafale_kn": 16,
                   "ratio_rafale": 1.6, "dir_deg": 247, "dir": "OSO"}


def test_a_calm_hour_gets_no_gust_ratio(monkeypatch):
    """Dividing by a 0 kn mean is not a ratio, it is a crash."""
    monkeypatch.setattr(meteo, "_transport", mock(router(
        forecast=hourly_body([0] * 24, gusts=[3] * 24))))
    got = run(meteo.wind_forecast("47.8,-4.3", jour="2026-08-02", de=12, a=12))
    assert "ratio_rafale" not in got["modeles"][0]["heures"][0]


def test_knots_are_requested_from_the_api(monkeypatch):
    """Native `wind_speed_unit=kn` - no conversion here, hence no float drift."""
    seen = {}

    def handler(request):
        if "daily" in request.url.params:
            return httpx.Response(200, json={"daily": {
                "sunrise": ["2026-08-02T06:55"], "sunset": ["2026-08-02T21:51"]}})
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=hourly_body([10] * 24))

    monkeypatch.setattr(meteo, "_transport", mock(handler))
    run(meteo.wind_forecast("47.8,-4.3", jour="2026-08-02"))
    assert seen["wind_speed_unit"] == "kn"
    # `auto`, never the house zone: pinning Europe/Paris on a spot elsewhere
    # pushes sunset onto the next calendar day and collapses the window.
    assert seen["timezone"] == "auto"
    assert seen["start_date"] == seen["end_date"] == "2026-08-02"


# -- the circular mean ------------------------------------------------------

def test_mean_bearing_crosses_north_correctly():
    """The trap that makes an arithmetic mean unusable: 350° and 10° average to
    NORTH, not to south. On a shore break that is onshore versus offshore."""
    assert meteo._mean_bearing([350, 10]) == 0
    assert meteo._mean_bearing([90, 110]) == 100


def test_opposed_bearings_have_no_mean():
    assert meteo._mean_bearing([0, 180]) is None
    assert meteo._mean_bearing([]) is None


# -- the daylight window ----------------------------------------------------

def test_default_window_is_daylight(monkeypatch):
    monkeypatch.setattr(meteo, "_transport", mock(router(
        forecast=hourly_body([10] * 24))))
    got = run(meteo.wind_forecast("47.8,-4.3", jour="2026-08-02"))
    # sunrise 06:55 rounds inwards to 07, sunset 21:51 down to 21.
    assert got["creneau"] == "07:00–21:00 (jour clair)"
    assert [r["h"] for r in got["modeles"][0]["heures"]][0] == "07:00"
    assert [r["h"] for r in got["modeles"][0]["heures"]][-1] == "21:00"


def test_explicit_window_skips_the_daylight_call(monkeypatch):
    def handler(request):
        if "daily" in request.url.params:
            pytest.fail("an explicit window needs no sunrise lookup")
        return httpx.Response(200, json=hourly_body([10] * 24))

    monkeypatch.setattr(meteo, "_transport", mock(handler))
    got = run(meteo.wind_forecast("47.8,-4.3", jour="2026-08-02", de=14, a=18))
    assert got["creneau"] == "14:00–18:00"
    assert len(got["modeles"][0]["heures"]) == 5


def test_a_backwards_daylight_window_falls_back_to_the_full_day(monkeypatch):
    """Regression: forcing the house zone onto a spot in another one puts
    sunset on the NEXT calendar day (Quebec sets after midnight, Paris time).
    The window inverted and the whole answer collapsed to "créneau vide" -
    found by running the addon against Quebec for real, not by reading it.
    A full day is a poor answer; an empty one is a wrong answer.
    """
    monkeypatch.setattr(meteo, "_transport", mock(router(
        daily={"daily": {"sunrise": ["2026-08-02T11:20"], "sunset": ["2026-08-03T01:15"]}},
        forecast=hourly_body([10] * 24))))
    got = run(meteo.wind_forecast("46.81,-71.21", jour="2026-08-02"))
    assert "error" not in got
    assert got["creneau"] == "00:00–23:00"
    assert len(got["modeles"][0]["heures"]) == 24


def test_backwards_window_is_refused(monkeypatch):
    monkeypatch.setattr(meteo, "_transport", mock(router()))
    got = run(meteo.wind_forecast("47.8,-4.3", jour="2026-08-02", de=18, a=9))
    assert "créneau vide" in got["error"]


# -- dispersion -------------------------------------------------------------

def test_dispersion_appears_only_with_two_answers(monkeypatch):
    def handler(request):
        if "daily" in request.url.params:
            return httpx.Response(200, json={"daily": {
                "sunrise": ["2026-08-02T06:55"], "sunset": ["2026-08-02T21:51"]}})
        speed = 18 if "arome" in request.url.params["models"] else 11
        return httpx.Response(200, json=hourly_body([speed] * 24))

    monkeypatch.setattr(meteo, "_transport", mock(handler))
    got = run(meteo.wind_forecast("47.8,-4.3", jour="2026-08-02", modeles="arome_hd,ecmwf"))
    assert got["dispersion"]["ecart_kn"] == 7.0

    solo = run(meteo.wind_forecast("47.8,-4.3", jour="2026-08-02", modeles="ecmwf"))
    assert "dispersion" not in solo


# -- guard rails ------------------------------------------------------------

def test_too_many_models_is_refused(monkeypatch):
    monkeypatch.setattr(meteo, "_transport", mock(
        lambda r: pytest.fail("refused before any call")))
    got = run(meteo.wind_forecast("47.8,-4.3", modeles="a,b,c,d,e,f"))
    assert "au maximum" in got["error"]


def test_bad_date_is_refused_before_calling(monkeypatch):
    monkeypatch.setattr(meteo, "_transport", mock(
        lambda r: pytest.fail("refused before any call")))
    assert "illisible" in run(meteo.wind_forecast("47.8,-4.3", jour="samedi"))["error"]


def test_unknown_place_explains_the_alternatives(monkeypatch):
    monkeypatch.setattr(meteo, "_transport", mock(router(geocode={"results": []})))
    got = run(meteo.wind_forecast("le trou du bout de nulle part"))
    assert "ROSETTA_WIND_SPOTS" in got["error"]


def test_every_answer_names_its_source(monkeypatch):
    """CC-BY 4.0: attribution is a licence condition, not a courtesy."""
    monkeypatch.setattr(meteo, "_transport", mock(router()))
    got = run(meteo.wind_forecast("47.8,-4.3", jour="2026-08-02"))
    assert "Open-Meteo" in got["source"] and "CC-BY" in got["source"]


# -- the surface is the guard -----------------------------------------------

def test_the_addon_is_read_only():
    """Open-Meteo has no write API, and no tool here may ever suggest one."""
    names = {t.name for t in run(meteo.mcp.list_tools())}
    assert names == {"wind_forecast", "wind_spots"}


def test_wind_spots_says_so_when_empty():
    got = run(meteo.wind_spots())
    assert got["spots"] == []
    assert "ROSETTA_WIND_SPOTS" in got["note"]
