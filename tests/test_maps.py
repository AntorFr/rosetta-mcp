"""maps addon: the weather tools and their wind.

`maps` shipped untested - it is the hub's oldest addon and was the only one
without a suite. The gap had a cost: the daily forecast dropped the `wind`
block Google was already billing for, silently, for months. These tests pin
the shaping so it cannot happen twice.

All against a mocked API (httpx.MockTransport, no network, no real key).
"""

import asyncio

import httpx
import pytest

from rosetta.addons import maps


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    """Every tool short-circuits without a key; the suite is about the rest."""
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")


def mock(handler):
    return httpx.MockTransport(handler)


# A wind block in Google's own shape, as all three weather endpoints send it.
WIND = {
    "direction": {"degrees": 335, "cardinal": "NORTH_NORTHWEST"},
    "speed": {"value": 24, "unit": "KILOMETERS_PER_HOUR"},
    "gust": {"value": 47, "unit": "KILOMETERS_PER_HOUR"},
}


# -- direction arithmetic ---------------------------------------------------

def test_cardinal_is_derived_from_degrees_in_french():
    assert maps._cardinal_fr(0) == "N"
    assert maps._cardinal_fr(90) == "E"
    assert maps._cardinal_fr(180) == "S"
    # 270 is WEST for Google, OUEST here - the whole point of not translating
    # the enum: "O", never "W".
    assert maps._cardinal_fr(270) == "O"
    assert maps._cardinal_fr(335) == "NNO"


def test_cardinal_wraps_at_the_full_circle():
    # 360 must land back on N, not walk off the end of the 16-entry table.
    assert maps._cardinal_fr(360) == "N"
    assert maps._cardinal_fr(350) == "N"


def test_cardinal_without_a_bearing_is_none_not_north():
    assert maps._cardinal_fr(None) is None


# -- wind shaping -----------------------------------------------------------

def test_wind_is_flattened_with_gust_and_bearing():
    assert maps._wind(WIND) == {
        "wind_kmh": 24, "wind_gust_kmh": 47, "wind_deg": 335, "wind_dir": "NNO",
    }


def test_absent_wind_yields_nothing_at_all():
    assert maps._wind(None) == {}
    assert maps._wind({}) == {}


def test_a_missing_gust_is_omitted_never_calm():
    """An unforecast gust must not read as 0 km/h - that is a promise of smooth
    air the API never made."""
    shaped = maps._wind({"speed": {"value": 12}, "direction": {"degrees": 90}})
    assert "wind_gust_kmh" not in shaped
    assert shaped == {"wind_kmh": 12, "wind_deg": 90, "wind_dir": "E"}


# -- the proto3 midnight trap ----------------------------------------------

def test_midnight_survives_its_missing_hours_key():
    """proto3 JSON omits zero-valued scalars, so 00:00 arrives with no `hours`.
    Read as None it would blank one row out of every twenty-four."""
    assert maps._local_hour({"year": 2026, "month": 8, "day": 1}) == "2026-08-01T00:00"
    assert maps._local_hour({"year": 2026, "month": 8, "day": 1, "hours": 0}) == "2026-08-01T00:00"
    assert maps._local_hour({"year": 2026, "month": 8, "day": 1, "hours": 14}) == "2026-08-01T14:00"


def test_local_hour_without_a_date_is_none():
    assert maps._local_hour(None) is None
    assert maps._local_hour({"hours": 14}) is None


# -- weather_now ------------------------------------------------------------

def test_weather_now_reports_the_gust(monkeypatch):
    monkeypatch.setattr(maps, "_transport", mock(lambda r: httpx.Response(200, json={
        "weatherCondition": {"description": {"text": "Ciel dégagé"}},
        "temperature": {"degrees": 21},
        "relativeHumidity": 62,
        "wind": WIND,
    })))
    got = run(maps.weather_now("47.8,-4.3"))
    assert got["wind_kmh"] == 24
    assert got["wind_gust_kmh"] == 47
    assert got["wind_dir"] == "NNO"
    assert got["wind_deg"] == 335


# -- weather_forecast: the regression that started all this -----------------

def test_daily_forecast_carries_the_wind_it_used_to_drop(monkeypatch):
    """The `wind` block sits under `daytimeForecast`, in the same paid-for
    response. It was fetched and thrown away."""
    monkeypatch.setattr(maps, "_transport", mock(lambda r: httpx.Response(200, json={
        "forecastDays": [{
            "displayDate": {"year": 2026, "month": 8, "day": 1},
            "minTemperature": {"degrees": 14},
            "maxTemperature": {"degrees": 23},
            "daytimeForecast": {
                "weatherCondition": {"description": {"text": "Averses"}},
                "precipitation": {"probability": {"percent": 40}},
                "wind": WIND,
            },
        }],
    })))
    day = run(maps.weather_forecast("47.8,-4.3", days=1))["days"][0]
    assert day["wind_kmh"] == 24
    assert day["wind_gust_kmh"] == 47
    assert day["wind_dir"] == "NNO"
    assert day["temp_max_c"] == 23


# -- weather_hourly ---------------------------------------------------------

def hourly_response(count=3):
    return httpx.Response(200, json={"forecastHours": [
        {
            "displayDateTime": {"year": 2026, "month": 8, "day": 1, "hours": h},
            "weatherCondition": {"description": {"text": "Nuageux"}},
            "temperature": {"degrees": 18 + h},
            "precipitation": {"probability": {"percent": 10 * h},
                              "qpf": {"quantity": 0.4}},
            "cloudCover": 75,
            "wind": WIND,
        } for h in range(count)
    ]})


def test_hourly_shapes_local_hours_and_wind(monkeypatch):
    monkeypatch.setattr(maps, "_transport", mock(lambda r: hourly_response()))
    got = run(maps.weather_hourly("47.8,-4.3", hours=3))
    assert [h["heure"] for h in got["hours"]] == [
        "2026-08-01T00:00", "2026-08-01T01:00", "2026-08-01T02:00",
    ]
    assert got["hours"][0]["wind_gust_kmh"] == 47
    assert got["hours"][0]["precip_mm"] == 0.4
    assert got["hours"][0]["cloud_pct"] == 75


def test_hourly_is_capped_at_one_page(monkeypatch):
    """`pageSize` maxes out at 24 upstream. Asking for more must clamp rather
    than silently return a partial page with an unread `nextPageToken`."""
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return hourly_response()

    monkeypatch.setattr(maps, "_transport", mock(handler))
    run(maps.weather_hourly("47.8,-4.3", hours=240))
    assert seen["hours"] == "24"
    assert seen["pageSize"] == "24"


def test_hourly_floors_at_one_hour(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return hourly_response(1)

    monkeypatch.setattr(maps, "_transport", mock(handler))
    run(maps.weather_hourly("47.8,-4.3", hours=0))
    assert seen["hours"] == "1"


def test_hourly_carries_the_api_error(monkeypatch):
    monkeypatch.setattr(maps, "_transport", mock(lambda r: httpx.Response(
        403, json={"error": {"message": "Weather API has not been used"}})))
    got = run(maps.weather_hourly("47.8,-4.3"))
    assert "Weather API has not been used" in got["error"]


# -- the degraded path ------------------------------------------------------

def test_no_key_is_an_explicit_error_and_no_call(monkeypatch):
    """The addon mounts degraded rather than failing to import, so every tool
    must answer in words instead of calling with an empty key."""
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    monkeypatch.setattr(maps, "_transport", mock(
        lambda r: pytest.fail("no HTTP call expected without a key")))
    for got in (run(maps.weather_hourly("47.8,-4.3")),
                run(maps.weather_now("47.8,-4.3")),
                run(maps.weather_forecast("47.8,-4.3"))):
        assert "GOOGLE_MAPS_API_KEY" in got["error"]
