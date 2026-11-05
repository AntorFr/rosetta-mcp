"""Test de caractérisation de la surface de Rosetta (AD-filet-1).

Il lit le code livré, construit sa surface — outils, routes, forme de /health,
variables d'environnement — et la compare à `tests/surface.json`, l'instantané
versionné, écrit une fois depuis v0.29.0.

L'instantané n'est jamais régénéré par un refactor : un écart est un changement
de comportement, relu comme tel. Pour l'écrire (volontairement, jamais pour
« faire passer » le test) : `python tests/test_surface.py --write`.

Aucun réseau, aucune date : `create_app()` se joue avec `ROSETTA_AUTH=off` et
sans aucune clé d'API.
"""

from __future__ import annotations

import ast
import asyncio
import difflib
import importlib
import inspect
import json
import os
import pkgutil
import sys
from pathlib import Path

SNAPSHOT = Path(__file__).parent / "surface.json"
SRC = Path(__file__).resolve().parent.parent / "src" / "rosetta"

# Lectures d'environnement dont le nom n'est pas un littéral, connues et assumées.
# Toute autre lecture dynamique est une décision, pas un angle mort : le test échoue.
DYNAMIC_READS = {
    "main.py": "`discover_addons` lit les noms du `required_env` de chaque addon, "
               "déjà épinglés dans la section env de l'instantané.",
    "addons/transit.py": "`Provider.key` lit `key_env`, déjà épinglé par les "
                         "`Provider(...)` de ce même fichier.",
}


# --------------------------------------------------------------------------
# env : les sources, lues par `ast`
# --------------------------------------------------------------------------

def _is_os_attr(node: ast.AST, attr: str) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == attr
            and isinstance(node.value, ast.Name) and node.value.id == "os")


def _literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def scan_file(path: Path) -> tuple[set[str], list[str]]:
    """(noms littéraux lus, lectures non résolues « ligne: raison »)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    names: set[str] = set()
    unresolved: list[str] = []

    def take(arg: ast.AST | None, node: ast.AST, what: str) -> None:
        value = _literal(arg)
        if value is None:
            unresolved.append(f"ligne {node.lineno}: {what} avec un nom non littéral")
        else:
            names.add(value)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name in ("environ", "getenv", "environb", "getenvb", "*"):
                    unresolved.append(f"ligne {node.lineno}: `from os import {alias.name}`")
        if _is_os_attr(node, "getenv"):
            call = parent.get(node)
            if isinstance(call, ast.Call) and call.func is node and call.args:
                take(call.args[0], node, "os.getenv")
            else:
                unresolved.append(f"ligne {node.lineno}: os.getenv sans argument lisible")
        elif _is_os_attr(node, "environ"):
            up = parent.get(node)
            if isinstance(up, ast.Subscript) and up.value is node:
                take(up.slice, node, "os.environ[...]")
            elif isinstance(up, ast.Attribute) and up.attr == "get":
                call = parent.get(up)
                if isinstance(call, ast.Call) and call.func is up and call.args:
                    take(call.args[0], node, "os.environ.get")
                else:
                    unresolved.append(f"ligne {node.lineno}: os.environ.get sans argument lisible")
            else:
                unresolved.append(f"ligne {node.lineno}: usage de os.environ hors get/[...]")
        elif isinstance(node, ast.Assign):
            # `required_env = [...]` au niveau du module : chaque littéral.
            if (parent.get(node) is tree and any(isinstance(t, ast.Name) and t.id == "required_env"
                                                 for t in node.targets)):
                if isinstance(node.value, (ast.List, ast.Tuple)):
                    for elt in node.value.elts:
                        take(elt, node, "required_env")
                else:
                    unresolved.append(f"ligne {node.lineno}: required_env n'est pas une liste littérale")
        elif isinstance(node, ast.AnnAssign):
            if (parent.get(node) is tree and isinstance(node.target, ast.Name)
                    and node.target.id == "required_env" and node.value is not None):
                if isinstance(node.value, (ast.List, ast.Tuple)):
                    for elt in node.value.elts:
                        take(elt, node, "required_env")
                else:
                    unresolved.append(f"ligne {node.lineno}: required_env n'est pas une liste littérale")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "Provider":
                # Provider(name, base_url, key_env, key_missing_msg)
                arg = node.args[2] if len(node.args) > 2 else next(
                    (k.value for k in node.keywords if k.arg == "key_env"), None)
                take(arg, node, "Provider(...)")
    return names, unresolved


def scan_env() -> dict:
    env: dict[str, list[str]] = {}
    problems: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        names, unresolved = scan_file(path)
        if names:
            env[rel] = sorted(names)
        if rel not in DYNAMIC_READS:
            problems += [f"{rel} {u}" for u in unresolved]
    return {"env": env, "problems": problems}


# --------------------------------------------------------------------------
# tools, routes, health : le code joué
# --------------------------------------------------------------------------

def _addon_names() -> list[str]:
    import rosetta.addons as pkg
    return sorted(i.name for i in pkgutil.iter_modules(pkg.__path__) if not i.name.startswith("_"))


def _scrub_env() -> None:
    """Un environnement de test sans aucun secret ni configuration."""
    for key in list(os.environ):
        if key.startswith(("ROSETTA_", "SNCF_", "IDFM_", "GITHUB_", "WITHINGS_", "MAIL_",
                           "OVH_", "POSTIER_", "API_MAREE_", "GOOGLE_")):
            del os.environ[key]
    os.environ["ROSETTA_AUTH"] = "off"


def _tools() -> dict:
    out = {}
    for name in _addon_names():
        module = importlib.import_module(f"rosetta.addons.{name}")
        tools = asyncio.run(module.mcp.list_tools())
        out[name] = sorted(
            ({"name": t.name, "description": inspect.cleandoc(t.description) if t.description else t.description,
              "inputSchema": t.inputSchema}
             for t in tools),
            key=lambda t: t["name"],
        )
    return out


def _routes_and_health() -> tuple[dict, dict]:
    from starlette.routing import Mount, Route
    from starlette.testclient import TestClient

    from rosetta.main import create_app

    app = create_app()
    inner, config = app.app, app.config
    top, mounts = [], []
    for r in inner.routes:
        if isinstance(r, Mount):
            mounts.append(r.path)
        elif isinstance(r, Route):
            top.append({"path": r.path, "methods": sorted(r.methods or [])})
    addons = {}
    for name in _addon_names():
        module = sys.modules[f"rosetta.addons.{name}"]
        addons[name] = {
            "identity": getattr(module, "identity", "machine"),
            "extra_routes": sorted(
                [{"path": f"/{name}{s}", "methods": sorted(m)} for s, _e, m in getattr(module, "extra_routes", [])],
                key=lambda r: r["path"]),
            "open_paths": sorted(getattr(module, "open_paths", [])),
        }
    routes = {
        "routes": sorted(top, key=lambda r: r["path"]),
        "mounts": sorted(mounts),
        "addons": addons,
        "user_only_prefixes": sorted(config.user_only_prefixes),
        "open_prefixes": sorted(config.open_prefixes),
    }
    with TestClient(app) as client:
        body = client.get("/health").json()
    health = {
        "keys": {k: type(v).__name__ for k, v in sorted(body.items())},  # version : valeur exclue
        "status": body["status"],
        "addons": dict(sorted(body["addons"].items())),
    }
    return routes, health


def build_surface() -> dict:
    _scrub_env()
    routes, health = _routes_and_health()
    return {
        "tools": _tools(),
        "routes": routes,
        "health": health,
        "env": scan_env()["env"],
    }


def _normalise(surface: dict) -> dict:
    """Aller-retour JSON : ce qu'on compare est ce que le fichier contient."""
    return json.loads(json.dumps(surface, sort_keys=True, ensure_ascii=False))


def _render(surface: dict) -> str:
    return json.dumps(surface, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------
# les tests
# --------------------------------------------------------------------------

def test_no_unresolved_env_read():
    """Une lecture d'environnement sans nom littéral est une décision, pas un angle mort."""
    problems = scan_env()["problems"]
    assert not problems, "lectures d'environnement non résolues :\n  " + "\n  ".join(problems)


def test_dynamic_reads_allowlist_is_live():
    """Chaque exception listée existe encore : sinon la liste ment."""
    for rel in DYNAMIC_READS:
        _names, unresolved = scan_file(SRC / rel)
        assert unresolved, f"{rel} n'a plus de lecture dynamique : retirer l'exception"


def test_surface_matches_snapshot(monkeypatch):
    # Une copie jetable : _scrub_env() y efface les secrets sans toucher le vrai environnement.
    monkeypatch.setattr(os, "environ", os.environ.copy())
    actual = _normalise(build_surface())
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    differing = [s for s in sorted(set(actual) | set(expected)) if actual.get(s) != expected.get(s)]
    if differing:
        report = []
        for section in differing:
            a = _render({section: actual.get(section)}).splitlines()
            e = _render({section: expected.get(section)}).splitlines()
            report.append(f"--- section « {section} » (instantané → code)\n" + "\n".join(
                difflib.unified_diff(e, a, "tests/surface.json", "code actuel", lineterm="", n=2)))
        print("\n\n".join(report))
    assert not differing, "la surface diffère de tests/surface.json, sections : " + ", ".join(differing)


if __name__ == "__main__":
    if "--write" not in sys.argv:
        sys.exit("usage : python tests/test_surface.py --write  (écrit tests/surface.json)")
    SNAPSHOT.write_text(_render(_normalise(build_surface())), encoding="utf-8")
    print(f"écrit {SNAPSHOT}")
