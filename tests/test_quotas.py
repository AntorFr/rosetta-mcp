"""quotas addon: the credential store and its rotation, the scope refusal that a
`claude setup-token` earns, answer shaping driven by `limits[]`, the fallback to
the legacy keys, and the rule that a stale reading announces its age instead of
passing for fresh. All against a mocked Anthropic API (httpx.MockTransport)."""

import asyncio
import json
import time

import httpx
import pytest

from rosetta.addons import quotas
from rosetta.auth import current_claims


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ROSETTA_QUOTAS_DATA", str(tmp_path))
    monkeypatch.setenv("ROSETTA_EXTERNAL_URL", "https://rosetta.example.com")
    monkeypatch.setenv("TZ", "Europe/Paris")
    monkeypatch.delenv("ROSETTA_QUOTAS_OWNER", raising=False)
    monkeypatch.delenv("ROSETTA_QUOTAS_CLIENTS", raising=False)
    quotas._token_cache.clear()
    quotas._usage_cache.clear()
    quotas._locks.clear()
    quotas._transport = None
    current_claims.set(None)
    yield tmp_path
    quotas._transport = None


@pytest.fixture
def enrolled(isolated):
    users = isolated / "users"
    users.mkdir()
    (users / "sebastien.json").write_text(json.dumps({
        "providers": {"claude": {"refresh_token": "rt-1", "enrolled_at": 0}},
    }))
    current_claims.set({"sub": "sebastien"})
    return isolated


def run(coro):
    return asyncio.run(coro)


USAGE = {
    "five_hour": {"utilization": 42.0, "resets_at": "2099-01-01T20:00:00+00:00"},
    "seven_day": {"utilization": 5.0, "resets_at": "2099-01-08T10:00:00+00:00"},
    "seven_day_opus": None,
    "nimbus_quill": {"utilization": 0.0, "resets_at": None},
    "limits": [
        {"kind": "session", "percent": 42, "severity": "normal",
         "resets_at": "2099-01-01T20:00:00+00:00", "scope": None, "is_active": True},
        {"kind": "weekly_all", "percent": 5, "severity": "normal",
         "resets_at": "2099-01-08T10:00:00+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_scoped", "percent": 0, "severity": "normal",
         "resets_at": "2099-01-08T10:00:00+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
         "is_active": False},
    ],
    "extra_usage": {"is_enabled": False, "spend_limit_reached": True,
                    "disabled_reason": "org_level_disabled_until"},
    "spend": {"used": {"amount_minor": 309, "currency": "EUR", "exponent": 2},
              "limit": {"amount_minor": 100, "currency": "EUR", "exponent": 2},
              "percent": 100, "severity": "critical", "enabled": False,
              "disabled_reason": "org_level_disabled_until", "can_toggle": False},
    "seven_day_breakdown": {
        "window_started_at": "2099-01-01T10:00:00+00:00",
        "rows": [{"key": "claude_code", "display_name": "Claude Code", "percent": 100},
                 {"key": "chat", "display_name": "Chats", "percent": 0}]},
}


def api(usage=None, usage_status=200, refresh=None, refresh_status=200, seen=None):
    """A mocked api.anthropic.com. `seen` collects the requests for assertions."""
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path == "/v1/oauth/token":
            body = refresh if refresh is not None else {
                "access_token": "at-1", "expires_in": 3600}
            return httpx.Response(refresh_status, json=body)
        if request.url.path == "/api/oauth/usage":
            return httpx.Response(usage_status,
                                  json=usage if usage is not None else USAGE)
        return httpx.Response(404, json={})
    return httpx.MockTransport(handler)


# -- identity and enrolment ------------------------------------------------

MACHINE = {"sub": "oauth2:client:supervision"}


def test_a_call_without_any_identity_is_refused(isolated):
    current_claims.set(None)
    assert "identité absente" in run(quotas.quotas())["error"]


def test_a_machine_reads_the_only_enrolled_subject(enrolled):
    """A supervision pod has no human behind it - and when exactly one
    subscription is enrolled, whose gauges it wants cannot be ambiguous."""
    current_claims.set(MACHINE)
    quotas._transport = api()
    out = run(quotas.quotas())
    assert out["compteurs"][0]["pourcent"] == 42


def test_a_machine_reads_the_designated_owner(enrolled, monkeypatch):
    (enrolled / "users" / "emilie.json").write_text(json.dumps(
        {"providers": {"claude": {"refresh_token": "rt-e"}}}))
    monkeypatch.setenv("ROSETTA_QUOTAS_OWNER", "sebastien")
    current_claims.set(MACHINE)
    seen = []
    quotas._transport = api(seen=seen)
    run(quotas.quotas())
    refresh = next(r for r in seen if r.url.path == "/v1/oauth/token")
    assert json.loads(refresh.content)["refresh_token"] == "rt-1"


def test_several_enrolled_and_no_owner_is_an_error_not_a_guess(enrolled):
    """Serving a household member's personal budget by alphabetical order would
    be a leak wearing the costume of a default."""
    (enrolled / "users" / "emilie.json").write_text(json.dumps(
        {"providers": {"claude": {"refresh_token": "rt-e"}}}))
    current_claims.set(MACHINE)
    out = run(quotas.quotas())
    assert "ROSETTA_QUOTAS_OWNER" in out["error"]
    assert "compteurs" not in out


def test_the_client_allowlist_narrows_when_it_is_set(enrolled, monkeypatch):
    monkeypatch.setenv("ROSETTA_QUOTAS_CLIENTS", "alfred,nestor")
    current_claims.set(MACHINE)
    assert "ROSETTA_QUOTAS_CLIENTS" in run(quotas.quotas())["error"]
    current_claims.set({"sub": "oauth2:client:alfred"})
    quotas._transport = api()
    assert run(quotas.quotas())["compteurs"]


def test_unenrolled_user_gets_an_actionable_error(isolated):
    current_claims.set({"sub": "quelqu-un"})
    out = run(quotas.quotas())
    assert "quotas/enroll" in out["error"]
    assert "quelqu-un" in out["error"]


def test_unknown_provider_names_the_known_ones(enrolled):
    out = run(quotas.quotas(fournisseur="openai"))
    assert "openai" in out["error"] and "claude" in out["error"]


def test_fournisseurs_reports_enrolment_state(enrolled):
    out = run(quotas.quotas_fournisseurs())
    claude = next(f for f in out["fournisseurs"] if f["nom"] == "claude")
    assert claude["enrole"] is True
    assert out["enrolement"].endswith("/quotas/enroll")


# -- the scope trap --------------------------------------------------------

def test_setup_token_scope_refusal_is_explained(enrolled):
    """A 403 here is the single most confusing failure this addon can hit: the
    credential is valid, it just cannot read profiles. Say which credential
    works instead, or the reader re-mints the wrong one."""
    quotas._transport = api(usage_status=403, usage={"error": {"type": "permission_error"}})
    out = run(quotas.quotas())
    assert "user:profile" in out["error"]
    assert "setup-token" in out["error"]


# -- shaping ---------------------------------------------------------------

def test_limits_array_drives_the_answer(enrolled):
    quotas._transport = api()
    out = run(quotas.quotas())
    assert out["lu_dans"] == "limits[]"
    compteurs = {c["compteur"]: c for c in out["compteurs"]}
    assert compteurs["fenêtre de 5 h"]["pourcent"] == 42
    assert compteurs["fenêtre de 5 h"]["en_cours"] is True
    assert compteurs["semaine (modèle dédié)"]["modele"] == "Fable"
    # Reset times are rendered where the household lives, not in UTC.
    assert compteurs["fenêtre de 5 h"]["remise_a_zero"] == "2099-01-01 21:00"


def test_an_unknown_bucket_is_passed_through_not_dropped(enrolled):
    payload = dict(USAGE, limits=[{"kind": "monthly_whatever", "percent": 7,
                                   "resets_at": "2099-02-01T00:00:00+00:00"}])
    quotas._transport = api(usage=payload)
    out = run(quotas.quotas())
    assert out["compteurs"][0]["compteur"] == "monthly_whatever"
    assert out["compteurs"][0]["pourcent"] == 7


def test_credits_block_says_whether_the_safety_net_is_on(enrolled):
    quotas._transport = api()
    credits = run(quotas.quotas())["credits_supplementaires"]
    assert credits["actif"] is False
    assert credits["depense"] == "3.09 EUR" and credits["plafond"] == "1.00 EUR"
    assert credits["motif_desactivation"] == "org_level_disabled_until"


def test_weekly_breakdown_drops_the_zero_rows(enrolled):
    quotas._transport = api()
    out = run(quotas.quotas())
    assert out["semaine_par_surface"]["repartition"] == {"Claude Code": 100}


def test_legacy_payload_falls_back_and_says_so(enrolled):
    payload = {k: v for k, v in USAGE.items() if k != "limits"}
    quotas._transport = api(usage=payload)
    out = run(quotas.quotas())
    assert "historiques" in out["lu_dans"]
    assert {c["compteur"] for c in out["compteurs"]} == {
        "fenêtre de 5 h", "semaine (tous modèles)"}


def test_unrecognizable_payload_warns_instead_of_answering_empty(enrolled):
    quotas._transport = api(usage={"something_entirely_new": {}})
    out = run(quotas.quotas())
    assert "compteurs" not in out
    assert "format amont a changé" in out["avertissement"]


# -- credential lifecycle --------------------------------------------------

def test_rotated_refresh_token_is_persisted_before_use(enrolled):
    quotas._transport = api(refresh={"access_token": "at-1", "expires_in": 3600,
                                     "refresh_token": "rt-2"})
    run(quotas.quotas())
    stored = json.loads((enrolled / "users" / "sebastien.json").read_text())
    assert stored["providers"]["claude"]["refresh_token"] == "rt-2"
    assert stored["providers"]["claude"]["refreshed_at"] > 0


def test_revoked_credential_asks_for_re_enrolment(enrolled):
    quotas._transport = api(refresh={"error": "invalid_grant"}, refresh_status=400)
    out = run(quotas.quotas())
    assert "ré-enrôlement" in out["error"]


def test_the_user_agent_is_sent(enrolled):
    """Without it the account lands in an anonymous, permanently 429 bucket."""
    seen = []
    quotas._transport = api(seen=seen)
    run(quotas.quotas())
    usage = next(r for r in seen if r.url.path == "/api/oauth/usage")
    assert usage.headers["user-agent"].startswith("claude-code/")
    assert usage.headers["anthropic-beta"] == "oauth-2025-04-20"


def test_a_second_call_is_served_from_cache(enrolled):
    seen = []
    quotas._transport = api(seen=seen)
    run(quotas.quotas())
    run(quotas.quotas())
    assert sum(1 for r in seen if r.url.path == "/api/oauth/usage") == 1


# -- staleness -------------------------------------------------------------

def test_rate_limited_read_returns_the_last_known_with_its_age(enrolled):
    quotas._usage_cache[("sebastien", "claude")] = (USAGE, time.time() - 300)
    quotas._transport = api(usage_status=429, usage={"error": {}})
    out = run(quotas.quotas())
    assert out["compteurs"], "les chiffres connus doivent survivre à un 429"
    assert out["obsolete_depuis"] == "5 min"
    assert "datés" in out["avertissement"]


def test_a_too_old_reading_is_not_resurrected(enrolled):
    quotas._usage_cache[("sebastien", "claude")] = (USAGE, time.time() - 7200)
    quotas._transport = api(usage_status=429, usage={"error": {}})
    out = run(quotas.quotas())
    assert "compteurs" not in out and "error" in out


# -- enrolment -------------------------------------------------------------
#
# The riskiest path of the addon: it writes a credential, and it is the one
# place where a `setup-token` gets caught. The probe-then-keep order and its
# rollback are what keep a refused credential from poisoning every later call.

def _enrol_client():
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    app = Starlette(routes=[Route("/quotas/enroll", quotas.enroll,
                                  methods=["GET", "POST"])])
    return TestClient(app)


SSO = {"Remote-User": "sebastien"}


def test_enrolment_page_refuses_an_unidentified_browser(isolated):
    r = _enrol_client().get("/quotas/enroll")
    assert r.status_code == 403 and "authentification" in r.text


def test_enrolment_page_carries_the_way_to_get_the_token(isolated):
    r = _enrol_client().get("/quotas/enroll", headers=SSO)
    assert r.status_code == 200
    # The page must name the trap, or the reader mints a setup-token and loses
    # an afternoon to a 403 whose cause is nowhere written down.
    assert "setup-token" in r.text and "CLAUDE_CONFIG_DIR" in r.text


def test_a_working_credential_is_probed_then_kept(isolated):
    quotas._transport = api()
    r = _enrol_client().post("/quotas/enroll", headers=SSO,
                             data={"refresh_token": "rt-1"})
    assert r.status_code == 200 and "enrôlé" in r.text
    stored = json.loads((isolated / "users" / "sebastien.json").read_text())
    assert stored["providers"]["claude"]["refresh_token"] == "rt-1"


def test_a_refused_credential_is_not_kept(isolated):
    """A stored credential that cannot read turns every later call into a
    puzzle - so a failed probe must leave the store exactly as it was."""
    quotas._transport = api(usage_status=403, usage={})
    r = _enrol_client().post("/quotas/enroll", headers=SSO,
                             data={"refresh_token": "un-setup-token"})
    assert r.status_code == 400
    assert "user:profile" in r.text and "setup-token" in r.text
    stored = json.loads((isolated / "users" / "sebastien.json").read_text())
    assert "claude" not in (stored.get("providers") or {})


def test_a_refused_credential_does_not_erase_the_working_one(enrolled):
    quotas._transport = api(usage_status=403, usage={})
    _enrol_client().post("/quotas/enroll", headers=SSO,
                         data={"refresh_token": "un-mauvais"})
    stored = json.loads((enrolled / "users" / "sebastien.json").read_text())
    assert stored["providers"]["claude"]["refresh_token"] == "rt-1"


# -- what a stolen machine token buys ---------------------------------------
#
# The point of opening the tools to machine identities: a supervision pod has no
# human behind it. The bound on that opening is that a reading is ALL a machine
# token can obtain here. These three tests are that bound, written down.

def test_the_addon_never_calls_anything_but_the_two_read_endpoints(enrolled):
    """The guarantee is the surface: no tool here sends a prompt, so a stolen
    machine token cannot turn this addon into a way to drive Claude - nor to
    spend a single token of the subscription it reports on."""
    current_claims.set(MACHINE)
    seen = []
    quotas._transport = api(seen=seen)
    run(quotas.quotas())
    quotas._usage_cache.clear()
    run(quotas.quotas(fournisseur="claude"))
    run(quotas.quotas_fournisseurs())
    run(quotas.quotas(fournisseur="openai"))
    paths = {r.url.path for r in seen}
    assert paths == {"/v1/oauth/token", "/api/oauth/usage"}
    assert "/v1/messages" not in paths


def test_no_answer_ever_carries_the_credential(enrolled):
    """It is a FULL account credential. An agent holding it would no longer be
    bounded by the tool surface at all - so it must not appear in an answer,
    including in the answers that report a failure."""
    current_claims.set(MACHINE)
    answers = []
    quotas._transport = api(refresh={"access_token": "at-secret", "expires_in": 3600,
                                     "refresh_token": "rt-rotated"})
    answers.append(run(quotas.quotas()))
    answers.append(run(quotas.quotas_fournisseurs()))
    quotas._usage_cache.clear()
    quotas._token_cache.clear()
    quotas._transport = api(refresh={"error": "invalid_grant"}, refresh_status=400)
    answers.append(run(quotas.quotas()))          # le chemin d'erreur aussi
    rendered = json.dumps(answers, ensure_ascii=False)
    for secret in ("rt-1", "rt-rotated", "at-secret"):
        assert secret not in rendered


def test_a_machine_token_cannot_enrol_or_replace_a_credential(enrolled):
    """Enrolment is not behind the hub JWT at all - it is behind the ingress
    SSO. So opening the TOOLS to machine identities does not open this."""
    current_claims.set(MACHINE)
    r = _enrol_client().post("/quotas/enroll", data={"refresh_token": "le-mien"})
    assert r.status_code == 403
    stored = json.loads((enrolled / "users" / "sebastien.json").read_text())
    assert stored["providers"]["claude"]["refresh_token"] == "rt-1"


# -- one subject, two spellings --------------------------------------------

@pytest.fixture
def enrolled_accented(isolated):
    """`_safe()` folds the accent for the filename, so one subject reaches the
    module as « Sébastien » (human, from Authelia) and as « S_bastien »
    (machine, read back from the filename). Both must be the same subject."""
    users = isolated / "users"
    users.mkdir()
    (users / "S_bastien.json").write_text(json.dumps({
        "sub": "Sébastien",
        "providers": {"claude": {"refresh_token": "rt-1", "enrolled_at": 0}},
    }))
    return isolated


def test_a_human_and_a_machine_share_one_cache_and_one_lock(enrolled_accented):
    """The serialization that stops a rotated refresh token from being burned
    twice is keyed on the subject. Two spellings resolving to ONE file must not
    hold two locks - that is precisely where the two kinds of caller meet."""
    seen = []
    quotas._transport = api(seen=seen)
    current_claims.set({"sub": "Sébastien"})
    run(quotas.quotas())
    current_claims.set(MACHINE)
    run(quotas.quotas())
    assert sum(1 for r in seen if r.url.path == "/api/oauth/usage") == 1
    assert len(quotas._usage_cache) == 1, "un seul sujet, un seul cache"
    assert len(quotas._token_cache) == 1, "un seul sujet, un seul jeton en cache"


def test_the_answer_keeps_the_real_spelling_not_the_filename(enrolled_accented):
    current_claims.set(MACHINE)
    assert run(quotas.quotas_fournisseurs())["utilisateur"] == "Sébastien"
