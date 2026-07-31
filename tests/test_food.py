"""food addon: the four silent traps of the Open Food Facts API (status in the
body under two different HTTP codes, mandatory field projection, an HTML answer
when saturated, `ecoscore_grade` as the live key), the per-IP quota, and the
read-only surface. All against a mocked API (httpx.MockTransport, no network)."""

import asyncio
import time

import httpx
import pytest

from rosetta.addons import food


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def fresh_quota(monkeypatch):
    """A fresh limiter per test: the window is process-wide state, so without
    this the 16th call of the whole suite would sleep for a minute."""
    monkeypatch.setattr(food, "_product_quota", food._Quota(15, 60.0, "product"))
    monkeypatch.setattr(food, "_search_quota", food._Quota(10, 60.0, "search"))


def mock(handler):
    return httpx.MockTransport(handler)


# A real Nutella answer, trimmed to the projected fields (values verified live).
NUTELLA = {
    "code": "3017620422003",
    "product_name": "Nutella",
    "product_name_fr": "Nutella",
    "brands": "Nutella, Ferrero",
    "quantity": "",                     # OFF fills unknowns with a blank
    "serving_size": None,
    "ingredients_text_fr": "Sucre, huile de palme, NOISETTES 13%…",
    # Taxonomy ids, which is what a projected call really returns (trap 5).
    "allergens_tags": ["en:milk", "en:nuts", "en:soybeans"],
    "traces_tags": [],
    "labels_tags": ["en:no-gluten", "en:some-unmapped-label"],
    "additives_tags": ["en:e322", "en:e322i"],
    "nutriscore_grade": "e",
    "nova_group": 4,
    "ecoscore_grade": "unknown",        # present, but means nothing
    "nutrient_levels": {"fat": "high", "salt": "low"},
    "nutriments": {"energy-kcal_100g": 539, "fat_100g": 30.9,
                   "sugars_100g": 56.3, "salt_100g": 0.107},
    "image_front_url": "https://images.openfoodfacts.org/front.jpg",
    "completeness": 0.7875,
}


def product_response(product=None, status=1, code=200, verbose="product found"):
    body = {"code": "x", "status": status, "status_verbose": verbose}
    if product is not None:
        body["product"] = product
    return httpx.Response(code, json=body)


# -- code parsing -----------------------------------------------------------

def test_split_codes_accepts_a_basket_and_rejects_noise():
    valid, rejected = food.split_codes("3017620422003, 3229820782560 12345678\nbonjour 42")
    assert valid == ["3017620422003", "3229820782560", "12345678"]
    # A scanner hands over noise now and then; it is reported, never queried.
    assert rejected == ["bonjour", "42"]


def test_split_codes_dedupes_and_takes_lists():
    valid, _ = food.split_codes(["3017620422003", "3017620422003", 3229820782560])
    assert valid == ["3017620422003", "3229820782560"]


def test_empty_input_is_an_error_not_a_call(monkeypatch):
    monkeypatch.setattr(food, "_transport", mock(lambda r: pytest.fail("no call expected")))
    assert "error" in run(food.food_product("   "))


# -- shaping ----------------------------------------------------------------

def test_product_shaping_keeps_facts_and_drops_holes(monkeypatch):
    monkeypatch.setattr(food, "_transport", mock(lambda r: product_response(NUTELLA)))
    out = run(food.food_product("3017620422003"))["produits"][0]

    assert out["trouve"] is True
    assert out["nom"] == "Nutella"
    assert out["nutriscore"] == "E"
    assert out["nova"] == 4 and out["nova_libelle"] == "ultra-transformé"
    assert out["additifs"] == ["E322", "E322I"]
    assert out["allergenes"] == ["lait", "fruits à coque", "soja"]
    # Unknown taxonomy entry: readable slug, never an invented translation.
    assert out["labels"] == ["sans gluten", "some unmapped label"]
    assert out["nutriments_100g"]["énergie"] == "539 kcal"
    assert out["nutriments_100g"]["sel"] == "0.107 g"
    assert out["reperes"] == {"matières grasses": "élevé", "sel": "faible"}
    assert out["completude"] == 0.79
    assert out["fiche"].endswith("/produit/3017620422003")

    # A hole stays a hole: an empty quantity, a null serving size, an "unknown"
    # eco-score and an empty traces list must be ABSENT, never returned as ""
    # or 0 - the agent would read a blank as a measured value.
    for hollow in ("quantite", "portion", "ecoscore", "traces"):
        assert hollow not in out
    assert "fibres" not in out["nutriments_100g"]


def test_allergens_are_translated_not_passed_through(monkeypatch):
    """Trap 5: `fields=allergens` re-renders the taxonomy in English, even with
    lc=fr and even on the fr. subdomain (all three measured). So the addon asks
    for the stable `_tags` ids and translates them itself. Regressing to the
    rendered field would hand a French user "milk, nuts, soybeans"."""
    seen = {}

    def handler(request):
        seen["fields"] = request.url.params.get("fields")
        return product_response(NUTELLA)

    monkeypatch.setattr(food, "_transport", mock(handler))
    out = run(food.food_product("3017620422003"))["produits"][0]
    assert "allergens_tags" in seen["fields"]
    assert "allergens," not in seen["fields"]        # not the rendered string
    assert out["allergenes"] == ["lait", "fruits à coque", "soja"]


def test_ecoscore_grade_is_the_key_read(monkeypatch):
    """Trap 4: upstream renamed Eco-Score to Green-Score, but the API still
    serves `ecoscore_grade` and does NOT serve `environmental_score_grade`
    (checked live). Reading the new name would silently drop every grade."""
    product = dict(NUTELLA, ecoscore_grade="a", ecoscore_score=84,
                   environmental_score_grade=None)
    monkeypatch.setattr(food, "_transport", mock(lambda r: product_response(product)))
    out = run(food.food_product("3229820782560"))["produits"][0]
    assert out["ecoscore"] == "A" and out["ecoscore_points"] == 84


# -- the traps --------------------------------------------------------------

def test_missing_product_is_an_answer_under_http_200(monkeypatch):
    """Trap 1a: an invalid code answers 200 with status 0."""
    monkeypatch.setattr(food, "_transport", mock(
        lambda r: product_response(status=0, code=200, verbose="no code or invalid code")))
    out = run(food.food_product("00000000"))["produits"][0]
    assert out["trouve"] is False
    assert "erreur" not in out          # not a failure: a fact
    assert "invalid code" in out["detail"]


def test_missing_product_is_an_answer_under_http_404(monkeypatch):
    """Trap 1b: the same status 0, this time behind a 404. Treating the HTTP
    code as the verdict would call one of these two cases wrong."""
    monkeypatch.setattr(food, "_transport", mock(
        lambda r: product_response(status=0, code=404, verbose="product not found")))
    out = run(food.food_product("3000000000018"))["produits"][0]
    assert out["trouve"] is False and "erreur" not in out


def test_field_projection_is_always_requested(monkeypatch):
    """Trap 2: one unprojected product is 148 724 bytes and 365 keys. Losing
    the `fields` parameter in a refactor would not break anything visibly - it
    would quietly drown the agent's context, so it gets pinned here."""
    seen = {}

    def handler(request):
        seen["fields"] = request.url.params.get("fields")
        seen["lc"] = request.url.params.get("lc")
        return product_response(NUTELLA)

    monkeypatch.setattr(food, "_transport", mock(handler))
    run(food.food_product("3017620422003"))
    assert seen["lc"] == "fr"
    for expected in ("product_name_fr", "nutriscore_grade", "nova_group",
                     "ecoscore_grade", "nutriments", "completeness"):
        assert expected in seen["fields"]


def test_html_holding_page_is_reported_not_raised(monkeypatch):
    """Trap 3: saturated, Open Food Facts serves an HTML page. `.json()` would
    raise on markup - the agent must get a sentence it can relay instead."""
    monkeypatch.setattr(food, "_transport", mock(
        lambda r: httpx.Response(503, text="<!DOCTYPE html><html>…</html>")))
    out = run(food.food_product("3017620422003"))["produits"][0]
    assert "saturé" in out["erreur"]


def test_user_agent_identifies_the_app(monkeypatch):
    """Documented OFF policy: no custom User-Agent, treated as a bot."""
    seen = {}

    def handler(request):
        seen["ua"] = request.headers.get("User-Agent")
        return product_response(NUTELLA)

    monkeypatch.setattr(food, "_transport", mock(handler))
    run(food.food_product("3017620422003"))
    assert "Alfred" in seen["ua"]


def test_network_failure_is_carried_per_code(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(food, "_transport", mock(handler))
    out = run(food.food_product("3017620422003"))["produits"][0]
    assert "injoignable" in out["erreur"]


# -- basket + quota ---------------------------------------------------------

def test_basket_is_fetched_in_one_call(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return product_response(dict(NUTELLA, product_name=request.url.path))

    monkeypatch.setattr(food, "_transport", mock(handler))
    out = run(food.food_product("3017620422003 3229820782560 12345678"))
    assert len(out["produits"]) == 3 and len(calls) == 3


def test_oversized_basket_is_truncated_and_says_so(monkeypatch):
    monkeypatch.setattr(food, "_transport", mock(lambda r: product_response(NUTELLA)))
    codes = " ".join(str(30000000000 + i) for i in range(18))
    out = run(food.food_product(codes))
    # Silent truncation would look like a fully processed basket.
    assert len(out["produits"]) == food.MAX_CODES
    assert len(out["non_traites"]) == 3 and "note" in out


def test_quota_blocks_the_call_over_the_window():
    """The limiter is the whole point of the module: OFF bans per IP, and that
    IP is the cluster's, shared by every service here."""
    quota = food._Quota(2, 0.25, "test")

    async def scenario():
        start = time.monotonic()
        await quota.take()
        await quota.take()
        await quota.take()          # third one must wait out the window
        return time.monotonic() - start

    assert run(scenario()) >= 0.25


# -- search -----------------------------------------------------------------

def test_search_shapes_hits_and_folds_the_brand_in(monkeypatch):
    seen = {}

    def handler(request):
        seen["q"] = request.url.params.get("q")
        return httpx.Response(200, json={"count": 9275, "is_count_exact": True, "hits": [
            {"code": "3119820129482", "product_name": "Bjorg muesli",
             "brands": ["Bjorg"], "quantity": "375 g", "nutriscore_grade": "unknown"},
        ]})

    monkeypatch.setattr(food, "_transport", mock(handler))
    out = run(food.food_search("muesli", marque="Bjorg"))
    assert seen["q"] == "muesli Bjorg"
    hit = out["resultats"][0]
    assert hit["nom"] == "Bjorg muesli" and hit["marque"] == "Bjorg"
    assert "nutriscore" not in hit      # "unknown" is a hole, not a grade
    assert out["total"] == 9275


def test_capped_search_count_is_not_reported_as_a_total(monkeypatch):
    """The engine caps its counter at 10 000 and says so. Relaying that as a
    total would be an invented fact, so it is dropped instead."""
    monkeypatch.setattr(food, "_transport", mock(lambda r: httpx.Response(
        200, json={"count": 10000, "is_count_exact": False, "hits": []})))
    assert "total" not in run(food.food_search("muesli"))


def test_empty_search_never_leaves(monkeypatch):
    monkeypatch.setattr(food, "_transport", mock(lambda r: pytest.fail("no call expected")))
    assert "error" in run(food.food_search("  "))


# -- the contract -----------------------------------------------------------

def test_surface_is_read_only():
    """Open Food Facts is community-EDITABLE. The guard is that no writing tool
    exists: the agent cannot publish into a public database on the user's
    behalf, and no hook is needed to stop it. Adding one breaks this test."""
    names = {t.name for t in run(food.mcp.list_tools())}
    assert names == {"food_product", "food_search"}
    assert not any(w in n for n in names
                   for w in ("add", "edit", "write", "upload", "contribute", "delete"))


def test_addon_needs_no_secret():
    """No key, no account, no enrolment: `required_env` must stay empty, and
    the addon must never become user-data. If either changes, deployment
    changes with it (ExternalSecret, ingress) - so it is pinned."""
    assert getattr(food, "required_env", []) == []
    assert getattr(food, "identity", "machine") == "machine"
    assert not getattr(food, "extra_routes", [])
