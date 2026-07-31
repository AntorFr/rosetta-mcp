"""`github` addon - dépôts GitHub pour l'agent de code, classe user-data.

Contrat (la garde EST la surface d'outils — délibérément étroite) :
  lectures  : repo_list, repo_file, repo_tree, repo_commits, repo_search_code,
              repo_tags, actions_runs
  écritures : repo_commit (créer/modifier/SUPPRIMER en un commit atomique),
              repo_tag (poser la ref d'une release)

N'EXISTENT PAS, et c'est la garantie — pas un hook : création ou suppression de
dépôt, fork, suppression de branche, force-push, écriture d'issue ou de PR, accès
aux secrets d'Actions, aux réglages ou aux collaborateurs. En ouvrir un plus tard
= écrire l'outil ET relire la garde dans la même passe.

Identité : `identity = "user"` — le hub refuse les tokens machine sur /github,
donc chaque appel porte un `sub` humain (Authelia). Le credential GitHub est rangé
CÔTÉ SERVEUR, un fichier par sujet sous ROSETTA_GITHUB_DATA ; le pod appelant ne
le voit jamais. Enrôlement : passage navigateur unique (/github/enroll -> consent
GitHub -> /github/callback), gardé par le forwardAuth de l'ingress (en-tête
Remote-User), qui produit le refresh token par utilisateur.

⚠️ L'App doit avoir « Expire user authorization tokens » ACTIVÉ : sans ça GitHub
ne délivre aucun refresh token et il faudrait se réenrôler à chaque expiration.

Descriptions d'outils en français — c'est de l'UX de runtime pour les agents.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
import unicodedata
from urllib.parse import urlencode

import httpx
from starlette.responses import RedirectResponse

from ..auth import current_claims
from ._common import TIMEOUT, dig, enrol_page, new_server, remote_user

logging.getLogger("httpx").setLevel(logging.WARNING)

identity = "user"
required_env = ["GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"]

mcp = new_server("github")

API = "https://api.github.com"
AUTH_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
UA = "rosetta-github-addon"

_transport = None  # crochet de test : les tests injectent un httpx.MockTransport


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, transport=_transport)


def _page(glyph, title, message, status=200):
    return enrol_page("GitHub", glyph, title, message, status)


# --------------------------------------------------------------------------
# Magasin de credentials, par utilisateur, côté serveur uniquement
# --------------------------------------------------------------------------

def _data_dir() -> str:
    return os.environ.get("ROSETTA_GITHUB_DATA", "/data/github")


def _safe(sub: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", sub)[:64]


def _user_file(sub: str) -> str:
    return os.path.join(_data_dir(), "users", f"{_safe(sub)}.json")


def _oauth_client() -> dict | str:
    cid = os.environ.get("GITHUB_CLIENT_ID", "").strip()
    secret = os.environ.get("GITHUB_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        return ("configuration GitHub absente (GITHUB_CLIENT_ID / "
                "GITHUB_CLIENT_SECRET) : l'addon n'est pas provisionné.")
    return {"client_id": cid, "client_secret": secret}


def _current_sub() -> str | None:
    claims = current_claims.get()
    if not claims:
        return None
    value = claims.get("preferred_username") or claims.get("sub")
    return unicodedata.normalize("NFC", str(value)) if value else None


_token_cache: dict[str, tuple[str, float]] = {}


async def _access_token(sub: str) -> str | dict:
    """Un token GitHub vivant pour `sub`, ou un dict {'error': ...}."""
    cached = _token_cache.get(sub)
    if cached and time.time() < cached[1]:
        return cached[0]
    try:
        with open(_user_file(sub)) as f:
            user = json.load(f)
    except FileNotFoundError:
        base = os.environ.get("ROSETTA_EXTERNAL_URL", "https://rosetta.mcp.berard.me")
        return {"error": (
            f"aucun compte GitHub enrôlé pour « {sub} ». Ouvrir {base}/github/enroll "
            "dans un navigateur pour autoriser l'accès (une seule fois)."
        )}
    client = _oauth_client()
    if isinstance(client, str):
        return {"error": client}
    async with _client() as http:
        r = await http.post(TOKEN_URL, headers={"Accept": "application/json"}, data={
            "grant_type": "refresh_token",
            "refresh_token": user["refresh_token"],
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
        })
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or not data.get("access_token"):
        detail = data.get("error_description") or data.get("error") or f"HTTP {r.status_code}"
        if data.get("error") in ("bad_refresh_token", "invalid_grant"):
            return {"error": (f"l'autorisation GitHub de « {sub} » a expiré ou a été "
                              "révoquée : ré-enrôlement nécessaire (/github/enroll).")}
        return {"error": f"rafraîchissement du token GitHub impossible : {detail}"}
    # GitHub fait TOURNER le refresh token à chaque usage : ne pas restocker le
    # nouveau condamne l'utilisateur au ré-enrôlement dès le rafraîchissement
    # suivant. On réécrit le fichier avant de rendre le token.
    if data.get("refresh_token"):
        user["refresh_token"] = data["refresh_token"]
        user["refreshed_at"] = int(time.time())
        with open(_user_file(sub), "w") as f:
            json.dump(user, f)
        os.chmod(_user_file(sub), 0o600)
    token = data["access_token"]
    _token_cache[sub] = (token, time.time() + int(data.get("expires_in", 28800)) - 120)
    return token


async def _headers() -> dict | dict:
    """En-têtes authentifiés pour l'appelant, ou {'error': ...}."""
    sub = _current_sub()
    if not sub:
        return {"error": "identité utilisateur absente du contexte d'appel (token machine ?)."}
    token = await _access_token(sub)
    if isinstance(token, dict):
        return token
    return {"headers": {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": UA,
    }}


async def _api(method: str, path: str, **kw) -> dict:
    """Un appel API authentifié. Renvoie {'error': ...} plutôt que de lever :
    un agent lit mieux un message qu'une trace."""
    h = await _headers()
    if "error" in h:
        return h
    async with _client() as http:
        r = await http.request(method, f"{API}{path}", headers=h["headers"], **kw)
    if r.status_code == 404:
        return {"error": f"introuvable : {path} (dépôt privé hors périmètre de l'App ?)"}
    if r.status_code == 403:
        return {"error": ("refusé par GitHub (403) — permission absente de l'App. "
                          "Committer sous .github/workflows/ exige `workflows: write`.")}
    if r.status_code >= 400:
        try:
            detail = r.json().get("message", r.text[:200])
        except ValueError:
            detail = r.text[:200]
        return {"error": f"GitHub {r.status_code} : {detail}"}
    if r.status_code == 204 or not r.content:
        return {"ok": True}
    return r.json()


def _owner() -> str:
    return os.environ.get("GITHUB_OWNER", "AntorFr")


def _slug(repo: str) -> str:
    """« nom » ou « owner/nom » -> « owner/nom »."""
    return repo if "/" in repo else f"{_owner()}/{repo}"


# --------------------------------------------------------------------------
# Lectures
# --------------------------------------------------------------------------

@mcp.tool()
async def repo_list(limit: int = 100) -> dict:
    """Liste les dépôts accessibles (nom, description, branche par défaut, visibilité)."""
    data = await _api("GET", "/user/repos", params={"per_page": min(limit, 100), "sort": "pushed"})
    if isinstance(data, dict) and "error" in data:
        return data
    return {"repos": [{
        "nom": r["name"], "slug": r["full_name"], "prive": r["private"],
        "branche": r.get("default_branch"), "description": r.get("description"),
        "pousse_le": r.get("pushed_at"),
    } for r in data]}


@mcp.tool()
async def repo_file(repo: str, chemin: str, ref: str = "") -> dict:
    """Lit un fichier texte d'un dépôt. `ref` = branche, tag ou sha (défaut : branche par défaut)."""
    params = {"ref": ref} if ref else {}
    data = await _api("GET", f"/repos/{_slug(repo)}/contents/{chemin}", params=params)
    if "error" in data:
        return data
    if isinstance(data, list):
        return {"error": f"« {chemin} » est un dossier — utiliser repo_tree."}
    if data.get("encoding") != "base64":
        return {"error": f"contenu non décodable (encoding {data.get('encoding')})."}
    try:
        texte = base64.b64decode(data["content"]).decode("utf-8")
    except UnicodeDecodeError:
        return {"error": f"« {chemin} » est binaire : cet outil ne sert que le texte."}
    return {"chemin": chemin, "taille": data.get("size"), "sha": data.get("sha"), "contenu": texte}


@mcp.tool()
async def repo_tree(repo: str, ref: str = "", sous_dossier: str = "") -> dict:
    """Arborescence d'un dépôt à un ref donné (récursive, fichiers seuls)."""
    ref = ref or "HEAD"
    data = await _api("GET", f"/repos/{_slug(repo)}/git/trees/{ref}", params={"recursive": "1"})
    if "error" in data:
        return data
    chemins = [n["path"] for n in data.get("tree", []) if n.get("type") == "blob"]
    if sous_dossier:
        prefix = sous_dossier.rstrip("/") + "/"
        chemins = [p for p in chemins if p.startswith(prefix)]
    return {"ref": ref, "tronque": data.get("truncated", False), "fichiers": chemins}


@mcp.tool()
async def repo_commits(repo: str, chemin: str = "", limit: int = 20) -> dict:
    """Derniers commits d'un dépôt, ou d'un fichier si `chemin` est fourni."""
    params = {"per_page": min(limit, 100)}
    if chemin:
        params["path"] = chemin
    data = await _api("GET", f"/repos/{_slug(repo)}/commits", params=params)
    if isinstance(data, dict) and "error" in data:
        return data
    return {"commits": [{
        "sha": c["sha"][:8],
        "date": dig(c, "commit", "author", "date"),
        "auteur": dig(c, "commit", "author", "name"),
        "message": (dig(c, "commit", "message", default="") or "").split("\n")[0],
    } for c in data]}


@mcp.tool()
async def repo_tags(repo: str, limit: int = 20) -> dict:
    """Tags d'un dépôt, du plus récent au plus ancien — utile pour connaître la dernière version publiée."""
    data = await _api("GET", f"/repos/{_slug(repo)}/tags", params={"per_page": min(limit, 100)})
    if isinstance(data, dict) and "error" in data:
        return data
    return {"tags": [{"nom": t["name"], "sha": dig(t, "commit", "sha", default="")[:8]} for t in data]}


@mcp.tool()
async def repo_search_code(requete: str, repo: str = "", limit: int = 20) -> dict:
    """Recherche de code. `repo` restreint à un dépôt, sinon toute la flotte accessible."""
    q = f"{requete} repo:{_slug(repo)}" if repo else f"{requete} user:{_owner()}"
    data = await _api("GET", "/search/code", params={"q": q, "per_page": min(limit, 100)})
    if "error" in data:
        return data
    return {"total": data.get("total_count", 0), "resultats": [
        {"depot": dig(i, "repository", "full_name"), "chemin": i.get("path")}
        for i in data.get("items", [])
    ]}


@mcp.tool()
async def actions_runs(repo: str, limit: int = 10) -> dict:
    """Derniers runs GitHub Actions d'un dépôt : conclusion, branche, date."""
    data = await _api("GET", f"/repos/{_slug(repo)}/actions/runs", params={"per_page": min(limit, 100)})
    if "error" in data:
        return data
    return {"runs": [{
        "nom": r.get("name"), "statut": r.get("status"), "conclusion": r.get("conclusion"),
        "branche": r.get("head_branch"), "date": r.get("created_at"), "url": r.get("html_url"),
    } for r in data.get("workflow_runs", [])]}


# --------------------------------------------------------------------------
# Écritures — deux outils, pas un de plus
# --------------------------------------------------------------------------

@mcp.tool()
async def repo_commit(repo: str, message: str, fichiers: list[dict],
                      branche: str = "") -> dict:
    """Publie UN commit atomique sur `origin`.

    `fichiers` : liste de {chemin, contenu}. Un `contenu` à `null` SUPPRIME le
    fichier — la suppression n'est donc pas un outil séparé, c'est un commit,
    et le bouclier garde le commit, qui est la bonne unité.

    Passe par l'API Git Data (blob -> arbre -> commit -> ref) : tout arrive en
    une seule révision, ou rien n'arrive.
    """
    slug = _slug(repo)
    if not fichiers:
        return {"error": "aucun fichier : un commit vide n'a pas de sens."}
    if not message.strip():
        return {"error": "message de commit vide."}

    if not branche:
        meta = await _api("GET", f"/repos/{slug}")
        if "error" in meta:
            return meta
        branche = meta.get("default_branch", "main")

    ref = await _api("GET", f"/repos/{slug}/git/ref/heads/{branche}")
    if "error" in ref:
        return ref
    base_sha = dig(ref, "object", "sha")

    commit = await _api("GET", f"/repos/{slug}/git/commits/{base_sha}")
    if "error" in commit:
        return commit
    base_tree = dig(commit, "tree", "sha")

    entries, supprimes = [], 0
    for f in fichiers:
        chemin = (f or {}).get("chemin")
        if not chemin:
            return {"error": "chaque entrée de `fichiers` doit porter un `chemin`."}
        contenu = f.get("contenu")
        if contenu is None:
            # sha: null dans un arbre = suppression du chemin
            entries.append({"path": chemin, "mode": "100644", "type": "blob", "sha": None})
            supprimes += 1
        else:
            entries.append({"path": chemin, "mode": "100644", "type": "blob", "content": contenu})

    tree = await _api("POST", f"/repos/{slug}/git/trees",
                      json={"base_tree": base_tree, "tree": entries})
    if "error" in tree:
        return tree

    nouveau = await _api("POST", f"/repos/{slug}/git/commits",
                         json={"message": message, "tree": tree["sha"], "parents": [base_sha]})
    if "error" in nouveau:
        return nouveau

    maj = await _api("PATCH", f"/repos/{slug}/git/refs/heads/{branche}",
                     json={"sha": nouveau["sha"], "force": False})
    if "error" in maj:
        return maj

    return {
        "depot": slug, "branche": branche, "sha": nouveau["sha"][:8],
        "fichiers": len(fichiers), "supprimes": supprimes,
        "url": f"https://github.com/{slug}/commit/{nouveau['sha']}",
    }


@mcp.tool()
async def repo_tag(repo: str, tag: str, sha: str = "", branche: str = "") -> dict:
    """Pose un tag — le geste qui déclenche la CI, donc une release.

    Sans `sha`, pointe sur la tête de `branche` (défaut : branche par défaut).
    Ne déplace JAMAIS un tag existant : GitHub refuse, et c'est très bien ainsi.
    """
    slug = _slug(repo)
    if not tag.strip():
        return {"error": "nom de tag vide."}
    if not sha:
        if not branche:
            meta = await _api("GET", f"/repos/{slug}")
            if "error" in meta:
                return meta
            branche = meta.get("default_branch", "main")
        ref = await _api("GET", f"/repos/{slug}/git/ref/heads/{branche}")
        if "error" in ref:
            return ref
        sha = dig(ref, "object", "sha")

    res = await _api("POST", f"/repos/{slug}/git/refs",
                     json={"ref": f"refs/tags/{tag}", "sha": sha})
    if "error" in res:
        if "already exists" in str(res["error"]).lower():
            return {"error": f"le tag « {tag} » existe déjà sur {slug} — un tag ne se déplace pas."}
        return res
    return {"depot": slug, "tag": tag, "sha": sha[:8],
            "url": f"https://github.com/{slug}/releases/tag/{tag}"}


# --------------------------------------------------------------------------
# Enrôlement navigateur (une fois par utilisateur)
# --------------------------------------------------------------------------

def _state_key() -> bytes:
    client = _oauth_client()
    seed = client["client_secret"] if isinstance(client, dict) else "rosetta-github"
    return hashlib.sha256(seed.encode()).digest()


def _sign_state(sub: str) -> str:
    payload = f"{sub}|{int(time.time())}"
    sig = hmac.new(_state_key(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode().rstrip("=")


def _verify_state(state: str, ttl: int = 600) -> str | None:
    try:
        raw = base64.urlsafe_b64decode(state + "=" * (-len(state) % 4)).decode()
        sub, ts, sig = raw.rsplit("|", 2)
    except Exception:
        return None
    attendu = hmac.new(_state_key(), f"{sub}|{ts}".encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, attendu):
        return None
    if time.time() - int(ts) > ttl:
        return None
    return sub


_remote_user = remote_user  # cf. _common : la récupération latin-1 -> utf-8 y vit


async def enroll(request):
    sub = _remote_user(request)
    if not sub:
        return _page("🚪", "Accès refusé",
                     "Cette page passe par le SSO de la maison — pas par la porte de service.", 403)
    client = _oauth_client()
    if isinstance(client, str):
        return _page("🧩", "Configuration absente", client, 500)
    external = os.environ.get("ROSETTA_EXTERNAL_URL", "https://rosetta.mcp.berard.me").rstrip("/")
    params = {
        "client_id": client["client_id"],
        "redirect_uri": f"{external}/github/callback",
        "state": _sign_state(sub),
    }
    return RedirectResponse(f"{AUTH_URL}?{urlencode(params)}", status_code=302)


async def callback(request):
    state = request.query_params.get("state", "")
    code = request.query_params.get("code")
    sub = _verify_state(state)
    if not sub or not code:
        return _page("⏳", "Flux invalide ou expiré",
                     "Reprendre depuis /github/enroll — le lien n'est valable que dix minutes.", 400)
    client = _oauth_client()
    if isinstance(client, str):
        return _page("🧩", "Configuration absente", client, 500)
    external = os.environ.get("ROSETTA_EXTERNAL_URL", "https://rosetta.mcp.berard.me").rstrip("/")
    async with _client() as http:
        r = await http.post(TOKEN_URL, headers={"Accept": "application/json"}, data={
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "code": code,
            "redirect_uri": f"{external}/github/callback",
        })
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or not data.get("access_token"):
        detail = data.get("error_description") or data.get("error") or f"HTTP {r.status_code}"
        return _page("🛑", "Échange refusé par GitHub",
                     f"Détail : {detail}. Reprendre depuis /github/enroll.", 502)
    if not data.get("refresh_token"):
        return _page("⚙️", "Jetons non expirants",
                     "GitHub n'a pas délivré de refresh token. Activer « Expire user "
                     "authorization tokens » dans les réglages de l'App, puis réessayer.", 400)
    os.makedirs(os.path.join(_data_dir(), "users"), exist_ok=True)
    path = _user_file(sub)
    with open(path, "w") as f:
        json.dump({"sub": sub, "refresh_token": data["refresh_token"],
                   "enrolled_at": int(time.time())}, f)
    os.chmod(path, 0o600)
    _token_cache.pop(sub, None)
    return _page("🔏", "Compte enrôlé",
                 f"Le compte GitHub de <b>{sub}</b> est désormais au service de la maison. "
                 "Cette page peut être fermée.")


extra_routes = [("/enroll", enroll, ["GET"]), ("/callback", callback, ["GET"])]
open_paths = ["/enroll", "/callback"]
