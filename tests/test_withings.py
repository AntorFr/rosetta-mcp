"""withings addon: credential store with a ROTATING refresh token, the
status-in-the-body envelope, unit scaling, and tool shaping - all against a
mocked Withings API (httpx.MockTransport, no network)."""

import asyncio
import base64
import json

import httpx
import pytest

from rosetta.addons import withings
from rosetta.auth import current_claims


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ROSETTA_WITHINGS_DATA", str(tmp_path))
    monkeypatch.setenv("WITHINGS_CLIENT_ID", "cid")
    monkeypatch.setenv("WITHINGS_CLIENT_SECRET", "csec")
    monkeypatch.setenv("TZ", "Europe/Paris")
    withings._token_cache.clear()
    withings._refresh_locks.clear()
    return tmp_path


@pytest.fixture
def enrolled(data_dir):
    users = data_dir / "users"
    users.mkdir()
    (users / "sebastien.json").write_text(json.dumps({
        "sub": "sebastien", "userid": 42, "refresh_token": "rt-1",
        "scopes": ["user.metrics"], "enrolled_at": 0,
    }))
    current_claims.set({"sub": "sebastien"})
    return data_dir


def mock(handler):
    return httpx.MockTransport(handler)


def run(coro):
    return asyncio.run(coro)


def _token(refresh_token="rt-2"):
    """A Withings token answer: HTTP 200, the payload under `body`."""
    return httpx.Response(200, json={"status": 0, "body": {
        "access_token": "at-1", "expires_in": 10800,
        "refresh_token": refresh_token, "userid": 42, "scope": "user.metrics",
    }})


def _ok(body):
    return httpx.Response(200, json={"status": 0, "body": body})


def _group(measures, date=1753900000, attrib=0, timezone="Asia/Tokyo", **extra):
    return {"grpid": 1, "attrib": attrib, "date": date, "category": 1,
            "timezone": timezone, "measures": measures, **extra}


# -- credentials, rotation, envelope ---------------------------------------

def test_unenrolled_user_gets_actionable_error(data_dir):
    current_claims.set({"sub": "quelqu-un"})
    out = run(withings.withings_measures())
    assert "withings/enroll" in out["error"]


def test_measures_scaling_labels_and_token_rotation(enrolled, monkeypatch):
    """The three things that silently corrupt everything: the power-of-ten unit,
    the label of a bare type code, and the refresh token that must be replaced."""
    def handler(request):
        if request.url.path.endswith("/v2/oauth2"):
            body = request.read()
            assert b"action=requesttoken" in body and b"refresh_token=rt-1" in body
            return _token(refresh_token="rt-2")
        return _ok({"measuregrps": [_group([
            {"value": 78192, "type": 1, "unit": -3},
            {"value": 213, "type": 6, "unit": -1},
            {"value": 155, "type": 175, "unit": 0, "position": 10},
        ])]})

    monkeypatch.setattr(withings, "_transport", mock(handler))
    out = run(withings.withings_measures())
    measures = out["groups"][0]["measures"]
    assert measures[0] == {"type": "poids", "value": 78.192, "unit": "kg"}
    assert measures[1] == {"type": "taux de masse grasse", "value": 21.3, "unit": "%"}
    assert measures[2]["position"] == "jambe gauche"
    # The group's own timezone wins over the server's: Tokyo has no DST.
    assert out["groups"][0]["date"].endswith("+09:00")
    # The rotated refresh token is persisted - the one we just used is dead.
    stored = json.loads((enrolled / "users" / "sebastien.json").read_text())
    assert stored["refresh_token"] == "rt-2"


def test_access_token_is_cached_and_refresh_serialized(enrolled, monkeypatch):
    """Every refresh burns the stored token, so two concurrent calls must share
    ONE refresh - not race each other into a revoked credential."""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/v2/oauth2"):
            return _token()
        return _ok({"measuregrps": []})

    monkeypatch.setattr(withings, "_transport", mock(handler))

    async def two_at_once():
        return await asyncio.gather(withings.withings_measures(),
                                    withings.withings_devices())

    run(two_at_once())
    assert sum(1 for c in calls if c.endswith("/v2/oauth2")) == 1
    run(withings.withings_measures())  # still cached afterwards
    assert sum(1 for c in calls if c.endswith("/v2/oauth2")) == 1


def test_http_200_with_error_status_is_not_success(enrolled, monkeypatch):
    """Withings answers 200 for its failures too: the status in the BODY rules."""
    def handler(request):
        if request.url.path.endswith("/v2/oauth2"):
            return _token()
        return httpx.Response(200, json={"status": 601, "error": "Too Many Requests"})

    monkeypatch.setattr(withings, "_transport", mock(handler))
    out = run(withings.withings_measures())
    assert "601" in out["error"] and "groups" not in out


def test_revoked_token_retries_once_then_asks_for_re_enrolment(enrolled, monkeypatch):
    """A cached access token can die early: retry once with a forced refresh,
    and only then send the user back to the enrolment page."""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/v2/oauth2"):
            # The refresh itself is refused: the grant is gone for good.
            if len(calls) > 2:
                return httpx.Response(200, json={"status": 401, "error": "invalid_grant"})
            return _token()
        return httpx.Response(200, json={"status": 401, "error": "invalid token"})

    monkeypatch.setattr(withings, "_transport", mock(handler))
    out = run(withings.withings_measures())
    assert "ré-enrôlement" in out["error"]
    assert sum(1 for c in calls if c.endswith("/v2/oauth2")) == 2  # one retry, not a loop


# -- windows, filters, shaping ---------------------------------------------

def test_window_end_date_covers_the_whole_day(enrolled, monkeypatch):
    captured = {}

    def handler(request):
        if request.url.path.endswith("/v2/oauth2"):
            return _token()
        captured["body"] = request.read().decode()
        return _ok({"measuregrps": []})

    monkeypatch.setattr(withings, "_transport", mock(handler))
    run(withings.withings_measures(start="2026-07-01", end="2026-07-02"))
    fields = dict(p.split("=", 1) for p in captured["body"].split("&"))
    # Two full days minus a second: an end date given as a day must include it.
    assert int(fields["enddate"]) - int(fields["startdate"]) == 172799


def test_measure_type_filter_accepts_names_aliases_and_codes(enrolled, monkeypatch):
    captured = {}

    def handler(request):
        if request.url.path.endswith("/v2/oauth2"):
            return _token()
        captured["body"] = request.read().decode()
        return _ok({"measuregrps": []})

    def sent():
        return dict(p.split("=", 1) for p in captured["body"].split("&"))

    monkeypatch.setattr(withings, "_transport", mock(handler))
    run(withings.withings_measures(types="poids, tension, 11"))
    # « tension » expands to systolic + diastolic, in that order.
    assert sent()["meastypes"] == "1%2C10%2C9%2C11"
    # Accent- and case-insensitive, and deduplicated.
    run(withings.withings_measures(types="POIDS, poids, Taux de masse grasse"))
    assert sent()["meastypes"] == "1%2C6"


def test_unknown_measure_type_is_refused_before_any_call(enrolled, monkeypatch):
    def handler(request):
        if request.url.path.endswith("/v2/oauth2"):
            return _token()
        raise AssertionError("no API call expected for an unknown type")

    monkeypatch.setattr(withings, "_transport", mock(handler))
    out = run(withings.withings_measures(types="tour de tête"))
    assert "inconnu" in out["error"] and "poids" in out["error"]


def test_bad_date_is_reported_not_swallowed(enrolled):
    out = run(withings.withings_measures(start="le 3 mars"))
    assert "date illisible" in out["error"]


def test_manual_entry_is_flagged(enrolled, monkeypatch):
    def handler(request):
        if request.url.path.endswith("/v2/oauth2"):
            return _token()
        return _ok({"measuregrps": [
            _group([{"value": 80, "type": 1, "unit": 0}], date=200, attrib=2),
            _group([{"value": 79, "type": 1, "unit": 0}], date=100, attrib=0),
        ]})

    monkeypatch.setattr(withings, "_transport", mock(handler))
    out = run(withings.withings_measures())
    assert out["groups"][0]["manual"] is True      # most recent first
    assert "manual" not in out["groups"][1]


def test_measures_are_capped_with_a_visible_note(enrolled, monkeypatch):
    def handler(request):
        if request.url.path.endswith("/v2/oauth2"):
            return _token()
        return _ok({"measuregrps": [
            _group([{"value": 80, "type": 1, "unit": 0}], date=i) for i in range(10)
        ]})

    monkeypatch.setattr(withings, "_transport", mock(handler))
    out = run(withings.withings_measures(max_results=3))
    assert len(out["groups"]) == 3
    assert "10 groupes" in out["note"]  # truncation is never silent


def test_sleep_summary_converts_and_drops_missing_fields(enrolled, monkeypatch):
    def handler(request):
        if request.url.path.endswith("/v2/oauth2"):
            return _token()
        assert b"action=getsummary" in request.read()
        return _ok({"series": [{
            "date": "2026-07-29", "startdate": 1753900000, "enddate": 1753930000,
            "timezone": "Europe/Paris",
            "data": {"sleep_score": 82, "total_sleep_time": 27000,
                     "deepsleepduration": 5400, "wakeupcount": 2,
                     "hr_average": 54, "apnea_hypopnea_index": None},
        }]})

    monkeypatch.setattr(withings, "_transport", mock(handler))
    night = run(withings.withings_sleep())["nights"][0]
    assert night["score"] == 82
    assert night["asleep_min"] == 450.0 and night["deep_min"] == 90.0
    assert night["wakeups"] == 2 and night["hr_average"] == 54
    assert "apnea_hypopnea_index" not in night  # absent, not zero
    assert "rem_min" not in night


def test_activity_shapes_a_day(enrolled, monkeypatch):
    def handler(request):
        if request.url.path.endswith("/v2/oauth2"):
            return _token()
        body = request.read()
        assert b"action=getactivity" in body and b"startdateymd" in body
        return _ok({"activities": [{
            "date": "2026-07-30", "steps": 8421, "distance": 6123.4,
            "calories": 412.5, "totalcalories": 2280, "active": 3600,
            "hr_average": 71,
        }]})

    monkeypatch.setattr(withings, "_transport", mock(handler))
    day = run(withings.withings_activity())["days"][0]
    assert day["steps"] == 8421 and day["distance_m"] == 6123
    assert day["active_min"] == 60.0 and day["hr_average"] == 71
    assert "elevation_m" not in day


def test_workouts_label_the_category(enrolled, monkeypatch):
    def handler(request):
        if request.url.path.endswith("/v2/oauth2"):
            return _token()
        assert b"action=getworkouts" in request.read()
        return _ok({"series": [{
            "category": 6, "date": "2026-07-28", "timezone": "Europe/Paris",
            "startdate": 1753900000, "enddate": 1753903600,
            "data": {"calories": 320, "distance": 15400.9, "hr_average": 128},
        }]})

    monkeypatch.setattr(withings, "_transport", mock(handler))
    session = run(withings.withings_workouts())["workouts"][0]
    assert session["activity"] == "vélo"
    assert session["duration_min"] == 60.0 and session["distance_m"] == 15401


def test_devices_report_battery(enrolled, monkeypatch):
    def handler(request):
        if request.url.path.endswith("/v2/oauth2"):
            return _token()
        assert b"action=getdevice" in request.read()
        return _ok({"devices": [
            {"type": "Scale", "model": "Body+", "battery": "low",
             "deviceid": "d1", "last_session_date": 1753900000,
             "first_session_date": None},
        ]})

    monkeypatch.setattr(withings, "_transport", mock(handler))
    device = run(withings.withings_devices())["devices"][0]
    assert device["battery"] == "low" and device["model"] == "Body+"
    assert "first_session" not in device


# -- the surface is the guard ----------------------------------------------

def test_addon_is_read_only():
    tool_names = {t.name for t in run(withings.mcp.list_tools())}
    assert tool_names == {
        "withings_measures", "withings_activity", "withings_sleep",
        "withings_workouts", "withings_devices",
    }
    # Pinned on purpose: Withings can write (goals, notifications, data deletion).
    # No tool here does, and none may slip in unnoticed.
    assert not any(v in n for n in tool_names
                   for v in ("create", "update", "delete", "set", "subscribe"))


# -- enrolment --------------------------------------------------------------

def test_state_sign_and_verify(data_dir):
    state = withings._sign_state("sebastien")
    assert withings._verify_state(state) == "sebastien"
    expiry, _sub, sig = base64.urlsafe_b64decode(state.encode()).decode().split(".", 2)
    forged = base64.urlsafe_b64encode(f"{expiry}.attacker.{sig}".encode()).decode()
    assert withings._verify_state(forged) is None
    assert withings._verify_state("garbage") is None


def test_enrolment_flow_end_to_end(data_dir, monkeypatch):
    from urllib.parse import parse_qs, urlparse

    from starlette.testclient import TestClient

    from rosetta.main import create_app

    monkeypatch.setenv("ROSETTA_AUTH", "oidc")  # auth ON: enroll stays open
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/withings/enroll", follow_redirects=False).status_code == 403
        r = client.get("/withings/enroll", headers={"Remote-User": "sebastien"},
                       follow_redirects=False)
        assert r.status_code == 302
        target = urlparse(r.headers["location"])
        assert target.hostname == "account.withings.com"
        query = parse_qs(target.query)
        # Comma-separated scopes: the space-separated OAuth form is rejected.
        assert query["scope"] == ["user.info,user.metrics,user.activity,user.sleepevents"]
        state = query["state"][0]

        def handler(request):
            assert b"grant_type=authorization_code" in request.read()
            return _token(refresh_token="rt-fresh")

        monkeypatch.setattr(withings, "_transport", mock(handler))
        r = client.get(f"/withings/callback?code=abc&state={state}")
        assert r.status_code == 200 and "sebastien" in r.text
    stored = json.loads((data_dir / "users" / "sebastien.json").read_text())
    assert stored["refresh_token"] == "rt-fresh" and stored["userid"] == 42


def test_enrolment_reports_a_refused_exchange(data_dir, monkeypatch):
    """status 0 is the only success - a 200 carrying status 503 is a failure."""
    from urllib.parse import parse_qs, urlparse

    from starlette.testclient import TestClient

    from rosetta.main import create_app

    monkeypatch.setenv("ROSETTA_AUTH", "oidc")
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/withings/enroll", headers={"Remote-User": "sebastien"},
                       follow_redirects=False)
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]

        def handler(request):
            return httpx.Response(200, json={"status": 503, "error": "Invalid Params"})

        monkeypatch.setattr(withings, "_transport", mock(handler))
        r = client.get(f"/withings/callback?code=abc&state={state}")
        assert r.status_code == 502 and "Invalid Params" in r.text
    assert not (data_dir / "users").exists()  # nothing half-written


def test_addon_degrades_without_client_credentials(tmp_path, monkeypatch):
    """A missing key must show on /health, not blow up at the first tool call."""
    monkeypatch.delenv("WITHINGS_CLIENT_ID", raising=False)
    monkeypatch.delenv("WITHINGS_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("ROSETTA_WITHINGS_DATA", str(tmp_path))

    from starlette.testclient import TestClient

    from rosetta.main import create_app

    monkeypatch.setenv("ROSETTA_AUTH", "off")
    with TestClient(create_app()) as client:
        health = client.get("/health").json()
    assert health["addons"]["withings"]["state"] == "degraded"
    assert "WITHINGS_CLIENT_ID" in health["addons"]["withings"]["detail"]
