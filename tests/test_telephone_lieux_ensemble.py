"""Ensemble de la feature rosetta-telephone-lieux, écrit depuis le besoin (fiche, messages « Livre »).

Besoin : chaque résultat de `search_places` (addon maps) porte le numéro de téléphone du lieu
quand Google en a un (internationalPhoneNumber, demandé dans le FieldMask), la description de
l'outil le dit ; un lieu sans numéro ne casse rien ; geocode reste intact.

Le hub réel (uvicorn rosetta.main:app, ROSETTA_AUTH=off, lancé par tests/hub_google_bouchon.py) est
interrogé en HTTP MCP sur /maps/ ; seul Google est bouchonné (httpx.MockTransport). Attendus écrits en dur.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

HEADERS = {"content-type": "application/json", "accept": "application/json, text/event-stream"}

LUIGI = {
    "id": "P1", "displayName": {"text": "Chez Luigi"}, "formattedAddress": "1 rue A, Nantes",
    "location": {"latitude": 47.2, "longitude": -1.5}, "rating": 4.5, "userRatingCount": 120,
    "currentOpeningHours": {"openNow": True}, "primaryTypeDisplayName": {"text": "Restaurant italien"},
    "internationalPhoneNumber": "+33 2 40 12 34 56",
}
KIOSQUE = {
    "id": "P2", "displayName": {"text": "Kiosque du port"}, "formattedAddress": "Quai B, Nantes",
    "location": {"latitude": 47.3, "longitude": -1.6},
}


REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def hub(tmp_path_factory):
    """Le vrai hub, lancé comme le README le dit (ROSETTA_AUTH=off), Google bouchonné."""
    tmp = tmp_path_factory.mktemp("hub")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items()
           if not k.endswith("_API_KEY") and not k.startswith("ROSETTA_")}
    env.update({
        "ROSETTA_AUTH": "off", "HOME": str(tmp), "GOOGLE_MAPS_API_KEY": "test-key",
        "ROSETTA_WITHINGS_DATA": str(tmp / "w"), "ROSETTA_GOOGLE_DATA": str(tmp / "g"),
        "ROSETTA_GITHUB_DATA": str(tmp / "gh"), "ROSETTA_QUOTAS_DATA": str(tmp / "q"),
        "BOUCHON_SCENARIO": str(tmp / "scenario.json"), "BOUCHON_JOURNAL": str(tmp / "journal.json"),
        "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO / "tests")]),
    })
    (tmp / "scenario.json").write_text('{"status": 200, "payload": {}}')
    proc = subprocess.Popen([sys.executable, str(REPO / "tests" / "hub_google_bouchon.py"), str(port)],
                            cwd=tmp, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                if httpx.get(base + "/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.3)
        else:
            raise RuntimeError("hub did not start")
        yield base, tmp
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture
def client(hub):
    return hub[0]


@pytest.fixture
def google(hub):
    """Fixe la réponse de Google ; renvoie le journal de la dernière requête reçue."""
    tmp = hub[1]
    (tmp / "journal.json").unlink(missing_ok=True)

    def install(status=200, payload=None):
        (tmp / "scenario.json").write_text(json.dumps({"status": status, "payload": payload or {}}))

        class Seen(dict):
            def __getitem__(self, k):
                return json.loads((tmp / "journal.json").read_text())[k]
        return Seen()
    return install


def call(client, name, arguments):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}
    r = httpx.post(client + "/maps/", json=body, headers=HEADERS, timeout=20)
    assert r.status_code == 200, r.text
    text = r.text
    if r.headers["content-type"].startswith("text/event-stream"):
        text = next(line[5:] for line in text.splitlines() if line.startswith("data:"))
    result = json.loads(text)["result"]
    if result.get("structuredContent"):
        sc = result["structuredContent"]
        return sc.get("result", sc)
    return json.loads(result["content"][0]["text"])


# nominal : un commerce connu rend son numéro
def test_recherche_d_un_commerce_connu_rend_son_numero(client, google):
    google(payload={"places": [LUIGI]})
    out = call(client, "search_places", {"query": "Chez Luigi", "near": "Nantes"})
    assert [r["name"] for r in out["results"]] == ["Chez Luigi"]
    assert out["results"][0]["phone"] == "+33 2 40 12 34 56"
    assert out["results"][0]["address"] == "1 rue A, Nantes"
    assert out["results"][0]["rating"] == 4.5


def test_google_est_interroge_avec_le_champ_telephone_et_les_champs_deja_demandes(client, google):
    seen = google(payload={"places": [LUIGI]})
    call(client, "search_places", {"query": "pizza"})
    fields = seen["mask"].split(",")
    assert "places.internationalPhoneNumber" in fields
    for f in ("places.displayName", "places.formattedAddress", "places.location", "places.id",
              "places.rating", "places.userRatingCount", "places.currentOpeningHours.openNow",
              "places.primaryTypeDisplayName"):
        assert f in fields


def test_la_description_de_l_outil_dit_qu_il_rend_le_telephone(client):
    r = httpx.post(client + "/maps/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                   headers=HEADERS, timeout=20)
    text = r.text
    if r.headers["content-type"].startswith("text/event-stream"):
        text = next(line[5:] for line in text.splitlines() if line.startswith("data:"))
    tools = {t["name"]: t for t in json.loads(text)["result"]["tools"]}
    desc = tools["search_places"]["description"].lower()
    assert "téléphone" in desc or "phone" in desc


# bords : sans numéro, mélange, plusieurs lieux, aucun lieu
def test_lieu_sans_numero_ne_casse_rien(client, google):
    google(payload={"places": [KIOSQUE]})
    out = call(client, "search_places", {"query": "kiosque"})
    assert "error" not in out
    res = out["results"][0]
    assert res["phone"] is None
    assert res["name"] == "Kiosque du port"
    assert res["address"] == "Quai B, Nantes"
    assert res["latlng"] == "47.3,-1.6"


def test_chaque_resultat_garde_son_propre_numero(client, google):
    google(payload={"places": [LUIGI, KIOSQUE]})
    out = call(client, "search_places", {"query": "nantes"})
    assert [(r["name"], r["phone"]) for r in out["results"]] == [
        ("Chez Luigi", "+33 2 40 12 34 56"), ("Kiosque du port", None)]


def test_aucun_lieu_rend_une_liste_vide(client, google):
    google(payload={})
    out = call(client, "search_places", {"query": "introuvable"})
    assert out["results"] == []


# échec : Google refuse
def test_refus_de_google_rend_l_erreur_sans_resultat(client, google):
    google(status=403, payload={"error": {"message": "API key invalid"}})
    out = call(client, "search_places", {"query": "pizza"})
    assert out == {"error": "API key invalid"}


# exclusions : geocode inchangé (ni numéro, ni champ téléphone demandé)
def test_geocode_ne_rend_pas_de_telephone_et_ne_le_demande_pas(client, google):
    seen = google(payload={"places": [LUIGI]})
    out = call(client, "geocode", {"address": "1 rue A, Nantes"})
    assert out == {"lat": 47.2, "lng": -1.5, "formatted": "1 rue A, Nantes", "place_id": "P1"}
    assert "internationalPhoneNumber" not in seen["mask"]
