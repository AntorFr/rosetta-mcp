"""github addon: rotating refresh token, the deliberately narrow tool surface,
and the atomic multi-file commit — all against a mocked GitHub API
(httpx.MockTransport, no network)."""

import asyncio
import json

import httpx
import pytest

from rosetta.addons import github
from rosetta.auth import current_claims


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ROSETTA_GITHUB_DATA", str(tmp_path))
    monkeypatch.setenv("GITHUB_CLIENT_ID", "Iv23licid")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "csec")
    monkeypatch.setenv("GITHUB_OWNER", "AntorFr")
    github._token_cache.clear()
    return tmp_path


@pytest.fixture
def enrolled(data_dir):
    users = data_dir / "users"
    users.mkdir()
    (users / "sebastien.json").write_text(json.dumps({
        "sub": "sebastien", "refresh_token": "rt-1", "enrolled_at": 0,
    }))
    current_claims.set({"sub": "sebastien"})
    return data_dir


def run(coro):
    return asyncio.run(coro)


def transport(handler):
    github._transport = httpx.MockTransport(handler)
    return github._transport


def token_ok(request):
    """Réponse d'échange/rafraîchissement : GitHub fait TOURNER le refresh token."""
    return httpx.Response(200, json={
        "access_token": "at-live", "refresh_token": "rt-2",
        "expires_in": 28800, "token_type": "bearer",
    })


# ---------------------------------------------------------------- credentials

def test_refresh_token_rotation_is_persisted(enrolled):
    """GitHub invalide l'ancien refresh token à chaque usage : ne pas restocker
    le nouveau condamnerait l'utilisateur au ré-enrôlement au tour suivant."""
    def handler(request):
        assert request.url.host == "github.com"
        return token_ok(request)

    transport(handler)
    tok = run(github._access_token("sebastien"))
    assert tok == "at-live"
    stored = json.loads((enrolled / "users" / "sebastien.json").read_text())
    assert stored["refresh_token"] == "rt-2", "le refresh token tourné doit être réécrit"


def test_not_enrolled_points_at_the_enrolment_page(data_dir):
    current_claims.set({"sub": "inconnu"})
    out = run(github._access_token("inconnu"))
    assert "error" in out and "/github/enroll" in out["error"]


def test_machine_token_is_refused(data_dir):
    """identity = "user" : sans sujet humain, aucun appel ne part."""
    current_claims.set(None)
    out = run(github._headers())
    assert "error" in out and "machine" in out["error"]


def test_missing_client_config_is_explicit(enrolled, monkeypatch):
    monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)
    out = run(github._access_token("sebastien"))
    assert "error" in out and "provisionn" in out["error"]


# ---------------------------------------------------------------- lectures

def test_repo_file_decodes_base64(enrolled):
    import base64 as b64

    def handler(request):
        if request.url.host == "github.com":
            return token_ok(request)
        assert request.url.path == "/repos/AntorFr/agent-pods/contents/README.md"
        return httpx.Response(200, json={
            "encoding": "base64", "size": 5, "sha": "abc",
            "content": b64.b64encode(b"salut").decode(),
        })

    transport(handler)
    out = run(github.repo_file("agent-pods", "README.md"))
    assert out["contenu"] == "salut"


def test_short_name_is_expanded_with_the_owner(enrolled):
    vu = {}

    def handler(request):
        if request.url.host == "github.com":
            return token_ok(request)
        vu["path"] = request.url.path
        return httpx.Response(200, json=[])

    transport(handler)
    run(github.repo_tags("rosetta-mcp"))
    assert vu["path"] == "/repos/AntorFr/rosetta-mcp/tags"


def test_403_names_the_workflows_permission(enrolled):
    """Le piège le plus coûteux de l'App : committer sous .github/workflows/
    exige une permission distincte de `contents`. Le message doit le dire."""
    def handler(request):
        if request.url.host == "github.com":
            return token_ok(request)
        return httpx.Response(403, json={"message": "Resource not accessible"})

    transport(handler)
    out = run(github.repo_tags("agent-pods"))
    assert "workflows" in out["error"]


# ---------------------------------------------------------------- écritures

def test_repo_commit_is_atomic_and_deletes_with_a_null(enrolled):
    """Un seul commit : blob -> arbre -> commit -> ref. `contenu: None` supprime."""
    calls = []

    def handler(request):
        if request.url.host == "github.com":
            return token_ok(request)
        p, m = request.url.path, request.method
        calls.append(f"{m} {p}")
        if p == "/repos/AntorFr/demo" and m == "GET":
            return httpx.Response(200, json={"default_branch": "main"})
        if p.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "base-sha"}})
        if p.endswith("/git/commits/base-sha"):
            return httpx.Response(200, json={"tree": {"sha": "tree-sha"}})
        if p.endswith("/git/trees") and m == "POST":
            body = json.loads(request.content)
            assert body["base_tree"] == "tree-sha"
            entries = {e["path"]: e for e in body["tree"]}
            assert entries["garde.py"]["content"] == "x = 1"
            assert entries["vieux.txt"]["sha"] is None, "sha null = suppression"
            return httpx.Response(201, json={"sha": "new-tree"})
        if p.endswith("/git/commits") and m == "POST":
            assert json.loads(request.content)["parents"] == ["base-sha"]
            return httpx.Response(201, json={"sha": "c0ffee1234"})
        if p.endswith("/git/refs/heads/main") and m == "PATCH":
            assert json.loads(request.content)["force"] is False, "jamais de force-push"
            return httpx.Response(200, json={})
        raise AssertionError(f"appel inattendu : {m} {p}")

    transport(handler)
    out = run(github.repo_commit("demo", "un commit", [
        {"chemin": "garde.py", "contenu": "x = 1"},
        {"chemin": "vieux.txt", "contenu": None},
    ]))
    assert out["sha"] == "c0ffee12" and out["supprimes"] == 1
    assert sum(1 for c in calls if c.startswith("POST")) == 2, "un seul arbre, un seul commit"


def test_repo_commit_refuses_an_empty_change(enrolled):
    out = run(github.repo_commit("demo", "rien", []))
    assert "error" in out and "vide" in out["error"]


def test_repo_tag_never_moves_an_existing_tag(enrolled):
    def handler(request):
        if request.url.host == "github.com":
            return token_ok(request)
        if request.url.path.endswith("/git/refs") and request.method == "POST":
            return httpx.Response(422, json={"message": "Reference already exists"})
        return httpx.Response(200, json={"object": {"sha": "s" * 40}, "default_branch": "main"})

    transport(handler)
    out = run(github.repo_tag("demo", "v1.0.0"))
    assert "existe déjà" in out["error"]


# ---------------------------------------------------------------- la surface

def test_the_surface_is_the_guard():
    """Ce qui n'est pas là est la GARANTIE, pas un oubli : aucun outil ne peut
    supprimer un dépôt, forker, écraser une branche, ni toucher aux secrets.
    Ce test échoue le jour où quelqu'un en ajoute un sans relire la garde."""
    exposed = {t for t in dir(github) if not t.startswith("_") and callable(getattr(github, t))}
    outils = exposed & {
        "repo_list", "repo_file", "repo_tree", "repo_commits", "repo_search_code",
        "repo_tags", "actions_runs", "repo_commit", "repo_tag",
    }
    assert len(outils) == 9, "la surface attendue est de 9 outils"
    interdits = [n for n in exposed if any(
        mot in n for mot in ("delete", "fork", "force", "secret", "collaborator",
                             "issue", "pull_request", "admin", "webhook")
    )]
    assert not interdits, f"outil hors contrat : {interdits}"


def test_identity_is_user_scoped():
    assert github.identity == "user", "un token machine ne doit jamais lire les dépôts"
    assert "GITHUB_CLIENT_SECRET" in github.required_env
