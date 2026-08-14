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

def test_repo_create_is_private_empty_and_under_the_caller(enrolled):
    """Toujours privé (le passage en public est un geste humain) et toujours
    VIDE : un README auto-généré rendrait le premier push d'un clone existant
    non fast-forward — que le proxy /git/ refuse. Le retour donne le remote
    rosetta prêt à câbler."""
    vu = {}

    def handler(request):
        if request.url.host == "github.com":
            return token_ok(request)
        assert request.method == "POST" and request.url.path == "/user/repos"
        vu["body"] = json.loads(request.content)
        return httpx.Response(201, json={
            "full_name": "AntorFr/atelier", "name": "atelier", "private": True,
            "html_url": "https://github.com/AntorFr/atelier",
        })

    transport(handler)
    out = run(github.repo_create("atelier", description="essai"))
    assert vu["body"]["private"] is True, "toujours privé"
    assert vu["body"]["auto_init"] is False, "toujours vide — le proxy interdit le force-push"
    assert out["depot"] == "AntorFr/atelier"
    assert out["remote_git"].endswith("/git/AntorFr/atelier")


def test_repo_create_refuses_an_owner_and_a_name_github_would_rewrite(enrolled):
    """Le dépôt naît sous le compte enrôlé — pas d'organisation, pas d'autre
    compte. Et un nom que GitHub normaliserait en silence (« mon repo » devient
    « mon-repo ») est refusé plutôt que créé au nom surprise."""
    assert "sans propriétaire" in run(github.repo_create("Org/naval"))["error"]
    assert "invalide" in run(github.repo_create("mon repo"))["error"]
    assert "invalide" in run(github.repo_create(".."))["error"]


def test_repo_create_is_pure_creation(enrolled):
    """Un nom déjà pris est un refus : rien n'est réutilisé, rien n'est écrasé."""
    def handler(request):
        if request.url.host == "github.com":
            return token_ok(request)
        return httpx.Response(422, json={"message": "name already exists on this account"})

    transport(handler)
    out = run(github.repo_create("agent-pods"))
    assert "existe déjà" in out["error"]


def test_repo_create_403_names_the_administration_permission(enrolled):
    """Le 403 de /user/repos ne vient ni de `workflows` ni de « Pull requests » :
    le message doit nommer « Administration » ET rappeler que le changement
    n'agit qu'approuvé côté installation — le piège du jour de la mise en place."""
    def handler(request):
        if request.url.host == "github.com":
            return token_ok(request)
        return httpx.Response(403, json={"message": "Resource not accessible by integration"})

    transport(handler)
    out = run(github.repo_create("atelier"))
    assert "Administration" in out["error"] and "installation" in out["error"]


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


# ---------------------------------------------------------------- pull requests

PR_OUVERTE = {
    "number": 1052, "title": "chore(deps): update unifi-network-mcp to v0.25.0",
    "user": {"login": "renovate[bot]"}, "draft": False, "state": "open",
    "merged": False, "mergeable": True, "mergeable_state": "clean",
    "commits": 1, "additions": 1, "deletions": 1, "body": "bump",
    "head": {"ref": "renovate/unifi-0.x", "sha": "f" * 40},
    "base": {"ref": "main"}, "updated_at": "2026-08-01T21:46:41Z",
    "html_url": "https://github.com/AntorFr/k8s-home-lab/pull/1052",
}


@pytest.fixture
def sans_attente(monkeypatch):
    """Les retries de mergeabilité ne doivent pas ralentir la suite."""
    monkeypatch.setattr(github, "_ATTENTE_MERGEABLE", 0)


def test_pull_requests_lists_the_freshest_first(enrolled):
    vu = {}

    def handler(request):
        if request.url.host == "github.com":
            return token_ok(request)
        vu["path"] = request.url.path
        vu["params"] = dict(request.url.params)
        return httpx.Response(200, json=[PR_OUVERTE])

    transport(handler)
    out = run(github.pull_requests("k8s-home-lab"))
    assert vu["path"] == "/repos/AntorFr/k8s-home-lab/pulls"
    assert vu["params"]["state"] == "open" and vu["params"]["sort"] == "updated"
    assert out["pull_requests"][0]["numero"] == 1052
    assert out["pull_requests"][0]["auteur"] == "renovate[bot]"


def test_unknown_state_is_refused_before_the_call(enrolled):
    out = run(github.pull_requests("demo", etat="merged"))
    assert "error" in out and "open, closed ou all" in out["error"]


def test_mergeable_null_is_retried_not_taken_for_a_no(enrolled, sans_attente):
    """LE piège : GitHub calcule la mergeabilité en tâche de fond et rend `null`
    à la première lecture d'une PR endormie. Rendu tel quel, un agent y lit « pas
    fusionnable » — un faux négatif silencieux sur une PR parfaitement saine."""
    lectures = []

    def handler(request):
        if request.url.host == "github.com":
            return token_ok(request)
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=[])
        lectures.append(1)
        if len(lectures) == 1:
            return httpx.Response(200, json=PR_OUVERTE | {"mergeable": None,
                                                          "mergeable_state": "unknown"})
        return httpx.Response(200, json=PR_OUVERTE)

    transport(handler)
    out = run(github.pull_request("k8s-home-lab", 1052))
    assert len(lectures) == 2, "la 2e lecture est ce que la doc GitHub prescrit"
    assert out["fusionnable"] is True
    assert out["etat_fusion_lisible"] == "prête à fusionner"
    assert "avertissement" not in out


def test_mergeable_still_null_warns_instead_of_lying(enrolled, sans_attente):
    def handler(request):
        if request.url.host == "github.com":
            return token_ok(request)
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=PR_OUVERTE | {"mergeable": None})

    transport(handler)
    out = run(github.pull_request("demo", 7))
    assert out["fusionnable"] is None
    assert "n'est PAS" in out["avertissement"], "« non calculé » n'est pas « non fusionnable »"


def test_diff_is_opt_in(enrolled, sans_attente):
    def handler(request):
        if request.url.host == "github.com":
            return token_ok(request)
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=[{
                "filename": "clusters/tantive/x.yml", "status": "modified",
                "additions": 1, "deletions": 1, "patch": "@@ -1 +1 @@\n-v1\n+v2",
            }])
        return httpx.Response(200, json=PR_OUVERTE)

    transport(handler)
    nu = run(github.pull_request("demo", 7))
    assert "patch" not in nu["fichiers"][0]
    avec = run(github.pull_request("demo", 7, diff=True))
    assert "+v2" in avec["fichiers"][0]["patch"]


def test_403_on_pulls_names_the_pull_requests_permission(enrolled):
    """Le 403 de /pulls ne vient PAS de `workflows` : le message doit nommer la
    bonne permission, sinon on cherche au mauvais endroit dans les réglages."""
    def handler(request):
        if request.url.host == "github.com":
            return token_ok(request)
        return httpx.Response(403, json={"message": "Resource not accessible"})

    transport(handler)
    out = run(github.pull_requests("demo"))
    assert "Pull requests" in out["error"] and "workflows" not in out["error"]


def test_merge_carries_the_head_sha_it_just_read(enrolled, sans_attente):
    """Sans `sha`, GitHub fusionnerait ce qui est en tête AU MOMENT du PUT. On
    passe la tête relue : la branche qui bouge entre-temps donne un 409, pas une
    fusion silencieuse de ce qu'on n'a pas regardé."""
    vu = {}

    def handler(request):
        if request.url.host == "github.com":
            return token_ok(request)
        if request.method == "PUT":
            vu["body"] = json.loads(request.content)
            return httpx.Response(200, json={"sha": "d" * 40, "merged": True})
        return httpx.Response(200, json=PR_OUVERTE)

    transport(handler)
    out = run(github.pull_request_merge("k8s-home-lab", 1052, methode="squash"))
    assert vu["body"] == {"merge_method": "squash", "sha": "f" * 40}
    assert out["sha"] == "dddddddd" and out["branche_fusionnee"] == "renovate/unifi-0.x"


def test_merge_refuses_a_draft_without_writing(enrolled, sans_attente):
    ecritures = []

    def handler(request):
        if request.url.host == "github.com":
            return token_ok(request)
        if request.method == "PUT":
            ecritures.append(1)
            return httpx.Response(200, json={"sha": "x", "merged": True})
        return httpx.Response(200, json=PR_OUVERTE | {"draft": True})

    transport(handler)
    out = run(github.pull_request_merge("demo", 7))
    assert "brouillon" in out["error"] and not ecritures, "aucun PUT ne doit partir"


def test_merge_refuses_a_conflicted_pr(enrolled, sans_attente):
    def handler(request):
        if request.url.host == "github.com":
            return token_ok(request)
        return httpx.Response(200, json=PR_OUVERTE | {"mergeable": False,
                                                      "mergeable_state": "dirty"})

    transport(handler)
    out = run(github.pull_request_merge("demo", 7))
    assert "conflits" in out["error"]


def test_merge_translates_the_409_into_a_moved_head(enrolled, sans_attente):
    def handler(request):
        if request.url.host == "github.com":
            return token_ok(request)
        if request.method == "PUT":
            return httpx.Response(409, json={"message": "Head branch was modified"})
        return httpx.Response(200, json=PR_OUVERTE)

    transport(handler)
    out = run(github.pull_request_merge("demo", 7))
    assert "a bougé" in out["error"] and "ffffffff" in out["error"]


def test_unknown_merge_method_is_refused(enrolled):
    out = run(github.pull_request_merge("demo", 7, methode="fast-forward"))
    assert "error" in out and "merge, squash ou rebase" in out["error"]


# ---------------------------------------------------------------- la surface

def test_the_surface_is_the_guard():
    """Ce qui n'est pas là est la GARANTIE, pas un oubli : aucun outil ne peut
    supprimer un dépôt, forker, écraser une branche, ouvrir/commenter/approuver
    une PR, ni toucher aux secrets. Ce test échoue le jour où quelqu'un en ajoute
    un sans relire la garde — hook `github_guard.py` du cockpit compris."""
    exposed = {t for t in dir(github) if not t.startswith("_") and callable(getattr(github, t))}
    outils = exposed & {
        "repo_list", "repo_file", "repo_tree", "repo_commits", "repo_search_code",
        "repo_tags", "actions_runs", "pull_requests", "pull_request",
        "repo_create", "repo_commit", "repo_tag", "pull_request_merge",
    }
    assert len(outils) == 13, "la surface attendue est de 13 outils"
    interdits = [n for n in exposed if any(
        mot in n for mot in ("delete", "fork", "force", "secret", "collaborator",
                             "issue", "admin", "webhook", "review", "approve",
                             "comment", "close")
    )]
    assert not interdits, f"outil hors contrat : {interdits}"


def test_enrolment_key_survives_an_accent():
    """Régression vécue en prod (2026-07-31). Les en-têtes HTTP sont du latin-1
    sur le fil, Authelia y met de l'UTF-8 : « Sébastien » arrive « SÃ©bastien ».
    Ce n'est PAS cosmétique — cette valeur est la CLÉ du magasin de credentials,
    que les appels relisent avec `preferred_username` issu du JWT, lui
    correctement décodé. Sans récupération, l'enrôlement range le credential sous
    une clé qu'aucun appel ne retrouvera, et l'erreur ne se voit qu'à l'usage."""
    class FauxRequest:
        def __init__(self, headers):
            self.headers = headers

    mutile = "Sébastien".encode("utf-8").decode("latin-1")
    assert mutile == "SÃ©bastien", "le cas de test doit reproduire la mutilation"
    assert github._remote_user(FauxRequest({"Remote-User": mutile})) == "Sébastien"
    # Un nom déjà propre ne doit pas être abîmé par la tentative de récupération.
    assert github._remote_user(FauxRequest({"Remote-User": "sebastien"})) == "sebastien"
    assert github._remote_user(FauxRequest({})) is None


def test_google_and_github_share_one_implementation():
    """La fonction avait été re-tapée dans `github`, sans le correctif. Une seule
    implémentation : le prochain addon hérite du correctif, pas du bug."""
    from rosetta.addons import _common, google
    assert github._remote_user is _common.remote_user
    assert google._remote_user is _common.remote_user


def test_identity_is_user_scoped():
    assert github.identity == "user", "un token machine ne doit jamais lire les dépôts"
    assert "GITHUB_CLIENT_SECRET" in github.required_env
