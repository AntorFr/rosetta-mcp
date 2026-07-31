"""`food` addon - Open Food Facts, read only, no key and no account.

Tools (descriptions intentionally in French - they are runtime UX for
French-speaking agents, see README):
  - food_product : barcode(s) -> name, brand, ingredients, allergens,
                   nutriments /100 g, Nutri-Score, NOVA, Eco-Score
  - food_search  : free-text search, the fallback when no barcode is at hand

Read access needs no credential at all: the addon carries no secret, no
enrolment, and stays `identity = "machine"`. Writing to Open Food Facts (the
database is community-editable) is deliberately NOT implemented - the agent
must not be able to publish into a public database on the user's behalf. As
everywhere in rosetta, the guard IS the surface: the capability does not exist.

Four traps, all verified against the live API on 2026-07-31 rather than
assumed - three of them fail silently:

1. A missing product answers with `status: 0`, under **either** HTTP 200
   (`0000000000000` -> "no code or invalid code") or HTTP 404
   (`3000000000018` -> "product not found"). The HTTP code alone tells you
   nothing; `status` in the body does. A missing product is not an error, it
   is an answer: `trouve: False`, so the agent can say "not in the database".
2. Projection with `fields=` is mandatory, not an optimisation. One product
   carries 365 keys in every language: 148 724 bytes raw, 252 bytes projected.
   Unprojected, a basket of fifteen would drown the agent's context.
3. Open Food Facts answers **HTML** when saturated (HTTP 503, "not available
   to anonymous users"). `r.json()` would raise on a page of markup; the
   caller gets an explicit "saturated" message instead of a stack trace.
4. `ecoscore_grade` is the live key, NOT `environmental_score_grade` - despite
   the upstream rename to Green-Score. The latter simply is not served (checked
   on a product that does carry a grade: Bjorg muesli -> ecoscore_grade "a").
5. Projection does not merely SELECT, it re-renders: asking for `fields=allergens`
   returns "milk, nuts, soybeans" where the unprojected document holds the
   product's own "lait, fruits a coque, soja" (`allergens_lc: fr`). Neither
   `lc=fr` nor the `fr.` subdomain changes it - all three were measured. So the
   `_tags` variants are requested instead and translated here, against the
   closed list of EU allergens. Getting this wrong would hand a French user an
   allergen list in English, which is where a mistake actually costs something.

And the one that bites the whole homelab: OFF rate-limits **per IP**, and the
cluster egress IP is shared by every service here. Hence `_Quota` below - the
limiter is the point of this module, not a nicety.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import deque

import httpx

from ._common import TIMEOUT, new_server

logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("rosetta.food")

mcp = new_server("food")

PRODUCT_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json"
SEARCH_URL = "https://search.openfoodfacts.org/search"
HUMAN_URL = "https://fr.openfoodfacts.org/produit/{code}"

# Documented policy: identify the app, or get treated as a bot.
USER_AGENT = os.environ.get("OFF_USER_AGENT", "Alfred/1.0 (contact@antor.fr)")

# One call may carry a whole shopping basket, but not an unbounded one: 15 is
# exactly one product-quota window, so a full basket never sleeps on an idle
# limiter. Above that, the agent splits - and the limiter throttles it anyway.
MAX_CODES = 15

# EAN-8 / EAN-13 / UPC-A and friends: digits only, 8 to 14 of them.
_CODE = re.compile(r"^\d{8,14}$")

# Injection point for tests (httpx.MockTransport); None = real network.
_transport = None


class _Quota:
    """Sliding-window limiter: at most `calls` starts per `window` seconds.

    Open Food Facts documents 15 product reads and 10 searches per minute *per
    IP address*, and warns that exceeding it earns an IP ban. The IP here is
    the cluster's egress, shared with every other service on the homelab - so
    the blast radius of a greedy basket is the whole house, not this addon.
    Waiting is therefore the correct behaviour, not an inconvenience.

    Process-wide state, which is only accurate because rosetta runs a single
    replica (see the deployment manifest); two replicas would each believe
    they own the full quota.
    """

    def __init__(self, calls: int, window: float, label: str):
        self._calls, self._window, self._label = calls, window, label
        self._starts: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def take(self) -> None:
        # The lock spans the wait on purpose: waiters queue up in FIFO order
        # instead of waking together and blowing the window as one.
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._starts and now - self._starts[0] >= self._window:
                    self._starts.popleft()
                if len(self._starts) < self._calls:
                    self._starts.append(now)
                    return
                delay = self._window - (now - self._starts[0]) + 0.05
                logger.info("quota %s reached, waiting %.1fs", self._label, delay)
                await asyncio.sleep(delay)


_product_quota = _Quota(15, 60.0, "product")
_search_quota = _Quota(10, 60.0, "search")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=TIMEOUT,
        transport=_transport,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )


def _payload(r: httpx.Response) -> dict | str:
    """The JSON body, or an explicit message. Open Food Facts serves an HTML
    holding page when saturated, so "did it parse" is a real question."""
    try:
        data = r.json()
    except ValueError:
        if r.status_code in (429, 503):
            return ("Open Food Facts est saturé ou refuse les requêtes anonymes "
                    "pour le moment — réessayer dans quelques minutes.")
        return f"réponse illisible d'Open Food Facts (HTTP {r.status_code})."
    if not isinstance(data, dict):
        return f"réponse inattendue d'Open Food Facts (HTTP {r.status_code})."
    return data


def _clean(value):
    """Open Food Facts fills unknown fields with empty strings and the literal
    "unknown"/"not-applicable" rather than omitting them. A hole must stay a
    hole: never hand the agent a blank it could read as a measured value."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value or value.lower() in ("unknown", "not-applicable", "none", "null"):
            return None
    if isinstance(value, (list, tuple)) and not value:
        return None
    return value


def _put(out: dict, key: str, value) -> None:
    value = _clean(value)
    if value is not None:
        out[key] = value


def _detag(tags, prefix_len: int = 3) -> list[str]:
    """"en:milk" -> "milk". OFF prefixes taxonomy entries with a language code."""
    out = []
    for t in tags or []:
        t = str(t)
        out.append(t[prefix_len:] if len(t) > prefix_len and t[2] == ":" else t)
    return out


# The 14 allergens the EU makes mandatory, plus the sub-tags Open Food Facts
# actually emits underneath them. A closed vocabulary, so translating it here
# is honest - see trap 5. Anything outside the table degrades to its readable
# slug rather than to a wrong French word.
ALLERGEN_FR = {
    "gluten": "gluten", "wheat": "blé", "barley": "orge", "oats": "avoine",
    "rye": "seigle", "spelt": "épeautre",
    "crustaceans": "crustacés", "eggs": "œufs", "fish": "poissons",
    "peanuts": "arachides", "soybeans": "soja", "milk": "lait",
    "nuts": "fruits à coque", "almonds": "amandes", "hazelnuts": "noisettes",
    "walnuts": "noix", "cashew-nuts": "noix de cajou",
    "pistachio-nuts": "pistaches", "macadamia-nuts": "noix de macadamia",
    "pecan-nuts": "noix de pécan", "brazil-nuts": "noix du Brésil",
    "celery": "céleri", "mustard": "moutarde", "sesame-seeds": "graines de sésame",
    "sulphur-dioxide-and-sulphites": "anhydride sulfureux et sulfites",
    "lupin": "lupin", "molluscs": "mollusques",
}

# Same treatment for the labels people actually look for. Open-ended taxonomy,
# so this is a courtesy layer, not a contract: the slug shows through otherwise.
LABEL_FR = {
    "organic": "bio", "eu-organic": "bio (UE)", "fr-bio-engagement": "AB",
    "ab-agriculture-biologique": "AB", "no-gluten": "sans gluten",
    "no-lactose": "sans lactose", "vegetarian": "végétarien", "vegan": "végan",
    "fair-trade": "commerce équitable", "green-dot": "point vert",
    "made-in-france": "fabriqué en France", "palm-oil-free": "sans huile de palme",
    "no-added-sugar": "sans sucres ajoutés", "high-fibres": "riche en fibres",
    "gluten-free": "sans gluten", "sugar-free": "sans sucre",
}

def _translate(tags, table: dict) -> list[str]:
    """Taxonomy ids -> French. An id the table does not know degrades to its
    readable slug ("no-added-sugar" -> "no added sugar"), never to a guess."""
    return [table.get(slug, slug.replace("-", " ")) for slug in _detag(tags)]


NOVA_LABEL = {
    1: "non transformé ou peu transformé",
    2: "ingrédient culinaire transformé",
    3: "aliment transformé",
    4: "ultra-transformé",
}

LEVEL_LABEL = {"low": "faible", "moderate": "modéré", "high": "élevé"}
LEVEL_NAME = {
    "fat": "matières grasses", "saturated-fat": "acides gras saturés",
    "sugars": "sucres", "salt": "sel",
}

# What a nutrition label actually carries, per 100 g, with its unit.
NUTRIMENTS = [
    ("energy-kcal_100g", "énergie", "kcal"),
    ("fat_100g", "matières grasses", "g"),
    ("saturated-fat_100g", "dont acides gras saturés", "g"),
    ("carbohydrates_100g", "glucides", "g"),
    ("sugars_100g", "dont sucres", "g"),
    ("fiber_100g", "fibres", "g"),
    ("proteins_100g", "protéines", "g"),
    ("salt_100g", "sel", "g"),
]

# Requested explicitly - see trap 2 in the module docstring.
PRODUCT_FIELDS = ",".join([
    "code", "product_name", "product_name_fr", "generic_name_fr", "brands",
    "quantity", "serving_size", "categories", "ingredients_text_fr",
    "ingredients_text", "additives_tags",
    # `_tags` rather than the rendered strings: see trap 5. These come back as
    # stable "en:"-prefixed taxonomy ids whatever the display language, which is
    # what makes translating them here reliable.
    "allergens_tags", "traces_tags", "labels_tags",
    "nutriscore_grade", "nova_group", "ecoscore_grade", "ecoscore_score",
    "nutrient_levels", "nutriments", "image_front_url", "completeness",
])

SEARCH_FIELDS = "code,product_name,brands,quantity,nutriscore_grade,nova_group"


def _shape(code: str, product: dict) -> dict:
    """Project one raw Open Food Facts product onto what an agent can use."""
    out: dict = {"code": code, "trouve": True}
    _put(out, "nom", product.get("product_name_fr") or product.get("product_name"))
    _put(out, "description", product.get("generic_name_fr"))
    _put(out, "marque", product.get("brands"))
    _put(out, "quantite", product.get("quantity"))
    _put(out, "portion", product.get("serving_size"))
    _put(out, "categories", product.get("categories"))
    # Labels are the noisiest taxonomy OFF serves (one real product came back
    # with sixteen, four of them restating the Nutri-Score we already report).
    # Trimmed: the grade has its own field, and a basket of fifteen products
    # should not spend its context on certifier reference numbers.
    labels = [x for x in _translate(product.get("labels_tags"), LABEL_FR)
              if not x.startswith("nutriscore")]
    _put(out, "labels", labels[:12])
    _put(out, "ingredients",
         product.get("ingredients_text_fr") or product.get("ingredients_text"))
    _put(out, "allergenes", _translate(product.get("allergens_tags"), ALLERGEN_FR))
    _put(out, "traces", _translate(product.get("traces_tags"), ALLERGEN_FR))
    _put(out, "additifs", [a.upper() for a in _detag(product.get("additives_tags"))])

    grade = _clean(product.get("nutriscore_grade"))
    _put(out, "nutriscore", grade.upper() if isinstance(grade, str) else None)

    nova = _clean(product.get("nova_group"))
    if nova is not None:
        try:
            nova = int(nova)
            out["nova"] = nova
            _put(out, "nova_libelle", NOVA_LABEL.get(nova))
        except (TypeError, ValueError):
            pass

    eco = _clean(product.get("ecoscore_grade"))
    _put(out, "ecoscore", eco.upper() if isinstance(eco, str) else None)
    _put(out, "ecoscore_points", product.get("ecoscore_score"))

    nutriments = product.get("nutriments") or {}
    values = {}
    for key, label, unit in NUTRIMENTS:
        value = nutriments.get(key)
        if isinstance(value, (int, float)):
            values[label] = f"{value:g} {unit}"
    _put(out, "nutriments_100g", values)

    levels = product.get("nutrient_levels") or {}
    _put(out, "reperes", {
        LEVEL_NAME.get(k, k): LEVEL_LABEL.get(v, v) for k, v in levels.items()
    })

    _put(out, "image", product.get("image_front_url"))
    completeness = product.get("completeness")
    if isinstance(completeness, (int, float)):
        # The user's own reservation about a community database, made into a
        # field: how filled-in this record is, so the agent can hedge.
        out["completude"] = round(float(completeness), 2)
    out["fiche"] = HUMAN_URL.format(code=code)
    return out


def split_codes(codes: str | list) -> tuple[list[str], list[str]]:
    """Accept one barcode or several (comma, space or newline separated).
    Returns (valid, rejected) - a scanner sometimes hands over noise."""
    if isinstance(codes, (list, tuple)):
        items = [str(c) for c in codes]
    else:
        items = re.split(r"[\s,;]+", str(codes or ""))
    valid, rejected, seen = [], [], set()
    for raw in items:
        code = raw.strip()
        if not code:
            continue
        if not _CODE.match(code):
            rejected.append(code)
        elif code not in seen:
            seen.add(code)
            valid.append(code)
    return valid, rejected


async def _fetch_one(client: httpx.AsyncClient, code: str) -> dict:
    await _product_quota.take()
    try:
        r = await client.get(PRODUCT_URL.format(code=code),
                             params={"fields": PRODUCT_FIELDS, "lc": "fr"})
    except httpx.HTTPError as exc:
        return {"code": code, "erreur": f"Open Food Facts injoignable : {exc}"}
    data = _payload(r)
    if isinstance(data, str):
        return {"code": code, "erreur": data}
    # Trap 1: status in the body decides, whatever the HTTP code says.
    if data.get("status") != 1 or not data.get("product"):
        return {"code": code, "trouve": False,
                "detail": data.get("status_verbose") or "produit absent de la base"}
    return _shape(code, data["product"])


@mcp.tool()
async def food_product(codes: str) -> dict:
    """Fiche produit alimentaire à partir d'un ou plusieurs codes-barres (Open Food Facts).

    codes : un code-barres (« 3017620422003 ») ou plusieurs, séparés par des
            virgules ou des espaces — un panier entier en un seul appel.
            15 au maximum ; au-delà, découper en plusieurs appels.

    Rend, quand l'information existe : nom, marque, quantité, ingrédients,
    allergènes, additifs, nutriments pour 100 g, Nutri-Score, groupe NOVA,
    Eco-Score, et `completude` (0 à 1).

    ⚠️ Base communautaire : la couverture est excellente sur les produits
    emballés vendus en France, inégale ailleurs. Un champ absent est OMIS —
    ne jamais lire « 0 » là où il n'y a rien, et se méfier d'une `completude`
    basse. Un produit inconnu rend `trouve: false` : ce n'est pas une panne,
    c'est une réponse.
    """
    valid, rejected = split_codes(codes)
    if not valid:
        return {"error": "aucun code-barres exploitable (8 à 14 chiffres attendus).",
                "rejetes": rejected} if rejected else {
            "error": "aucun code-barres fourni."}
    truncated = valid[MAX_CODES:]
    valid = valid[:MAX_CODES]

    # Bounded parallelism: the quota is the real bottleneck, but four in flight
    # turn an idle-window basket into one round trip instead of fifteen.
    gate = asyncio.Semaphore(4)

    async def guarded(client, code):
        async with gate:
            return await _fetch_one(client, code)

    async with _client() as client:
        produits = await asyncio.gather(*(guarded(client, c) for c in valid))

    out: dict = {"produits": list(produits)}
    if rejected:
        out["rejetes"] = rejected
    if truncated:
        out["non_traites"] = truncated
        out["note"] = (f"{MAX_CODES} codes au maximum par appel — relancer "
                       f"food_product sur les {len(truncated)} restants.")
    return out


@mcp.tool()
async def food_search(query: str, marque: str | None = None, max_results: int = 5) -> dict:
    """Recherche de produits alimentaires par texte libre (Open Food Facts).

    Le repli quand on n'a pas de code-barres : « muesli sans gluten »,
    « yaourt brassé vanille ». Rend code-barres, nom, marque, quantité,
    Nutri-Score et NOVA ; reprendre le code trouvé avec `food_product` pour la
    fiche complète.

    query : ce qu'on cherche.
    marque : optionnel, une marque à privilégier.
    max_results : nombre de résultats (défaut 5, plafonné à 25).

    ⚠️ Jamais au fil de la frappe : le quota amont est de 10 recherches par
    minute et par adresse IP, partagée par toute la maison.
    """
    text = " ".join(p for p in (str(query or "").strip(), (marque or "").strip()) if p)
    if not text:
        return {"error": "recherche vide."}
    max_results = max(1, min(int(max_results or 5), 25))

    await _search_quota.take()
    try:
        async with _client() as client:
            r = await client.get(SEARCH_URL, params={
                "q": text, "page_size": max_results, "fields": SEARCH_FIELDS,
                "countries_tags": "en:france", "langs": "fr",
            })
    except httpx.HTTPError as exc:
        return {"error": f"Open Food Facts injoignable : {exc}"}
    data = _payload(r)
    if isinstance(data, str):
        return {"error": data}

    results = []
    for hit in data.get("hits") or []:
        code = str(hit.get("code") or "")
        item: dict = {"code": code}
        _put(item, "nom", hit.get("product_name"))
        brands = hit.get("brands")
        _put(item, "marque", ", ".join(brands) if isinstance(brands, list) else brands)
        _put(item, "quantite", hit.get("quantity"))
        grade = _clean(hit.get("nutriscore_grade"))
        _put(item, "nutriscore", grade.upper() if isinstance(grade, str) else None)
        _put(item, "nova", hit.get("nova_group"))
        if code:
            item["fiche"] = HUMAN_URL.format(code=code)
        results.append(item)
    out = {"recherche": text, "resultats": results}
    # The engine caps its counter and flags it: a capped 10 000 reported as a
    # total would be an invented fact. Report the count only when it is exact.
    if data.get("is_count_exact"):
        out["total"] = data.get("count")
    return out


if __name__ == "__main__":
    # Local stdio debugging: `python -m rosetta.addons.food`.
    mcp.run()
