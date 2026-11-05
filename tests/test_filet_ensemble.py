"""Ensemble de la feature rosetta-qualite-filet (B-6, B-9, P-3, P-4), écrit depuis le besoin.

Deux volets, tous deux sur le vrai code, sans réseau :
- le hub est lancé comme le README le dit (uvicorn, ROSETTA_AUTH=off, aucune clé) et
  interrogé en HTTP : ce qu'un client voit de `transit` et de `/health` ;
- le test de caractérisation (`tests/test_surface.py`) est joué sur une copie du dépôt
  qu'on abîme : il doit rougir pour chaque changement de surface, et rester vert sinon.
Les attendus sont écrits en dur d'après docs/addons.md et docs/parcours.md.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent

MCP_HEADERS = {"content-type": "application/json", "accept": "application/json, text/event-stream"}


def _env(tmp: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.endswith("_API_KEY") and not k.startswith("ROSETTA_")}
    env.update({"ROSETTA_AUTH": "off", "HOME": str(tmp), **extra})
    for var in ("GOOGLE_CLIENT_ID", "WITHINGS_CLIENT_ID"):
        env.pop(var, None)
    return env


@pytest.fixture(scope="module")
def hub(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("hub")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = _env(tmp, ROSETTA_WITHINGS_DATA=str(tmp / "w"), ROSETTA_GOOGLE_DATA=str(tmp / "g"),
               ROSETTA_GITHUB_DATA=str(tmp / "gh"), ROSETTA_QUOTAS_DATA=str(tmp / "q"))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "rosetta.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=tmp, env={**env, "PYTHONPATH": str(REPO / "src")},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _mcp(base: str, path: str, method: str, params: dict | None = None) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    r = httpx.post(base + path, json=body, headers=MCP_HEADERS, timeout=20)
    assert r.status_code == 200, r.text
    text = r.text
    if r.headers["content-type"].startswith("text/event-stream"):
        text = next(line[5:] for line in text.splitlines() if line.startswith("data:"))
    return json.loads(text)


# --- B-6 / P-3 : ce qu'un agent client voit de transit --------------------------------

def test_transit_expose_exactement_quatre_outils(hub):
    tools = _mcp(hub, "/transit/", "tools/list")["result"]["tools"]
    assert sorted(t["name"] for t in tools) == [
        "metro_departures", "metro_disruptions", "train_departures", "train_disruptions"]


def test_transit_schemas_d_entree_des_departs(hub):
    tools = {t["name"]: t for t in _mcp(hub, "/transit/", "tools/list")["result"]["tools"]}
    for name in ("train_departures", "metro_departures"):
        schema = tools[name]["inputSchema"]
        assert sorted(schema["properties"]) == ["count", "station"]
        assert schema["required"] == ["station"]
        assert schema["properties"]["count"]["default"] == 5
        assert schema["properties"]["count"]["type"] == "integer"


def test_transit_schemas_d_entree_des_perturbations_station_optionnelle(hub):
    tools = {t["name"]: t for t in _mcp(hub, "/transit/", "tools/list")["result"]["tools"]}
    for name in ("train_disruptions", "metro_disruptions"):
        schema = tools[name]["inputSchema"]
        assert list(schema["properties"]) == ["station"]
        assert not schema.get("required")


def test_transit_description_des_outils_est_le_contrat(hub):
    tools = {t["name"]: t for t in _mcp(hub, "/transit/", "tools/list")["result"]["tools"]}
    assert tools["train_departures"]["description"].startswith(
        "Prochains départs SNCF depuis une gare, avec retard calculé (théorique vs temps réel).")
    assert tools["metro_disruptions"]["description"].startswith(
        "Perturbations RATP/IDFM en cours (incident, travaux).")


def test_transit_sans_cle_repond_une_erreur_explicite_et_le_hub_reste_debout(hub):
    out = _mcp(hub, "/transit/", "tools/call",
               {"name": "train_departures", "arguments": {"station": "Nantes"}})["result"]
    assert "SNCF_API_KEY" in json.dumps(out, ensure_ascii=False)
    assert httpx.get(hub + "/health", timeout=5).status_code == 200


def test_outil_inconnu_sur_transit_est_refuse(hub):
    out = _mcp(hub, "/transit/", "tools/call", {"name": "metro_passages", "arguments": {"station": "x"}})
    assert "error" in out or out["result"].get("isError") is True


# --- B-9 : /health et routes ----------------------------------------------------------

def test_health_sans_secret_est_degrade_et_nomme_transit(hub):
    r = httpx.get(hub + "/health", timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert body["addons"]["transit"]["state"] == "degraded"
    assert body["addons"]["transit"]["detail"] == "missing env: SNCF_API_KEY, IDFM_API_KEY"
    assert "food" in body["addons"] and body["addons"]["food"]["state"] == "ok"


def test_health_sans_jeton_mais_appel_d_outil_refuse_en_mode_oidc(tmp_path):
    # exclusion : /health reste non authentifié (README) ; une route d'addon, elle, ne l'est pas.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = _env(tmp_path, ROSETTA_ADDONS="food", ROSETTA_ISSUER="https://idp.example",
               ROSETTA_EXTERNAL_URL="https://rosetta.example")
    env.pop("ROSETTA_AUTH")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "rosetta.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=tmp_path, env={**env, "PYTHONPATH": str(REPO / "src")},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(base + "/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.3)
        else:
            pytest.fail("hub did not start")
        assert httpx.get(base + "/health").status_code == 200
        # tools/list est servi sans jeton (README), un appel d'outil non.
        r = httpx.post(base + "/food/", headers=MCP_HEADERS, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "food_search", "arguments": {"query": "x"}}})
        assert r.status_code == 401
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_routes_d_enrolement_withings_existent(hub):
    for path in ("/withings/enroll", "/withings/callback"):
        r = httpx.get(hub + path, follow_redirects=False, timeout=5)
        assert r.status_code != 404, path


def test_chemin_inconnu_reste_404(hub):
    assert httpx.get(hub + "/n-existe-pas", timeout=5).status_code == 404


# --- B-6 / B-9 : le test de caractérisation échoue quand la surface change ------------

@pytest.fixture()
def copie(tmp_path):
    dest = tmp_path / "repo"
    shutil.copytree(REPO, dest, ignore=shutil.ignore_patterns(
        ".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".coverage"))
    return dest


def _joue_surface(repo: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ROSETTA_") and not k.endswith("_API_KEY")}
    env["PYTHONPATH"] = str(repo / "src")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_surface.py"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=300)


def _remplace(path: Path, avant: str, apres: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert avant in text, f"{avant!r} introuvable dans {path.name}"
    path.write_text(text.replace(avant, apres, 1), encoding="utf-8")


def test_caracterisation_verte_sur_une_copie_intacte(copie):
    res = _joue_surface(copie)
    assert res.returncode == 0, res.stdout[-2000:]


def test_caracterisation_rouge_si_un_outil_est_renomme(copie):
    _remplace(copie / "src/rosetta/addons/transit.py",
              "async def metro_departures(", "async def metro_passages(")
    assert _joue_surface(copie).returncode != 0


def test_caracterisation_rouge_si_un_schema_d_entree_change(copie):
    _remplace(copie / "src/rosetta/addons/transit.py",
              "async def train_departures(station: str, count: int = 5)",
              "async def train_departures(station: str, count: int = 6)")
    assert _joue_surface(copie).returncode != 0


def test_caracterisation_rouge_si_un_parametre_est_ajoute(copie):
    _remplace(copie / "src/rosetta/addons/transit.py",
              "async def train_disruptions(station: str | None = None)",
              "async def train_disruptions(station: str | None = None, limite: int = 3)")
    assert _joue_surface(copie).returncode != 0


def test_caracterisation_rouge_si_une_docstring_d_outil_change(copie):
    _remplace(copie / "src/rosetta/addons/transit.py",
              "Prochains départs SNCF depuis une gare", "Prochains départs SNCF depuis une station")
    assert _joue_surface(copie).returncode != 0


def test_caracterisation_rouge_si_une_route_est_retiree(copie):
    _remplace(copie / "src/rosetta/addons/withings.py",
              'extra_routes = [("/enroll", enroll, ["GET"]), ("/callback", callback, ["GET"])]',
              'extra_routes = [("/callback", callback, ["GET"])]')
    assert _joue_surface(copie).returncode != 0


def test_caracterisation_rouge_si_une_variable_d_environnement_est_ajoutee(copie):
    path = copie / "src/rosetta/addons/food.py"
    path.write_text(path.read_text(encoding="utf-8")
                    + '\nimport os\n_NOUVELLE = os.environ.get("ROSETTA_VARIABLE_NOUVELLE", "")\n',
                    encoding="utf-8")
    assert _joue_surface(copie).returncode != 0


def test_caracterisation_rouge_si_une_cle_requise_est_ajoutee_a_transit(copie):
    _remplace(copie / "src/rosetta/addons/transit.py",
              'return os.environ.get(self.key_env, "")',
              'return os.environ.get(self.key_env, os.environ.get("TRANSIT_SECOURS", ""))')
    assert _joue_surface(copie).returncode != 0


def test_caracterisation_reste_verte_pour_un_changement_sans_effet_sur_la_surface(copie):
    # exclusion : un commentaire ou un corps de fonction modifié n'est pas un changement de surface.
    _remplace(copie / "src/rosetta/addons/transit.py",
              "    return await _departures(SNCF, station, count)",
              "    # corps modifié, contrat inchangé\n    return await _departures(SNCF, station, count)")
    res = _joue_surface(copie)
    assert res.returncode == 0, res.stdout[-2000:]
