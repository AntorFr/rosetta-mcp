"""`google` addon - Gmail + Calendar for the household agents, user-data class.

Contract (the guard IS the tool surface - deliberately narrow):
  - mail_search / mail_thread : read-only Gmail
  - mail_draft               : creates a DRAFT, never sends - no send tool exists
  - calendar_events / calendar_create / calendar_update : no delete tool exists

Identity: `identity = "user"` - the hub refuses machine tokens on /google, so
every call carries a human `sub` (Authelia). Google credentials are stored
SERVER-SIDE, one file per subject under ROSETTA_GOOGLE_DATA; agents never see
them. Enrolment is a one-time browser flow (/google/enroll -> Google consent ->
/google/callback), guarded by the ingress forwardAuth (Remote-User header),
which yields the per-user Google refresh token.

Tool descriptions are in French - runtime UX for the household agents.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from email.message import EmailMessage
from urllib.parse import urlencode

import httpx
from starlette.responses import HTMLResponse, RedirectResponse

from ..auth import current_claims
from ._common import TIMEOUT, dig, new_server

logging.getLogger("httpx").setLevel(logging.WARNING)

identity = "user"

mcp = new_server("google")

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR = "https://www.googleapis.com/calendar/v3"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

# gmail.compose is the narrowest scope that allows draft creation. It nominally
# permits sending too - the guarantee that no mail ever leaves is that NO send
# tool exists in this module and the credentials never leave the server.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.events",
]

DEFAULT_TZ = "Europe/Paris"

# Test hook: tests inject an httpx.MockTransport here.
_transport = None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, transport=_transport)


# --------------------------------------------------------------------------
# Per-user credential store (server-side only)
# --------------------------------------------------------------------------

def _data_dir() -> str:
    return os.environ.get("ROSETTA_GOOGLE_DATA", "/data/google")


def _safe(sub: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", sub)[:64]


def _user_file(sub: str) -> str:
    return os.path.join(_data_dir(), "users", f"{_safe(sub)}.json")


def _oauth_client() -> dict | str:
    """client_id/client_secret of the Google OAuth app (client_secret.json)."""
    path = os.path.join(_data_dir(), "client_secret.json")
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return f"configuration Google absente ({path}) : l'addon n'est pas provisionné."
    except Exception as exc:
        return f"client_secret.json illisible : {exc}"
    conf = raw.get("web") or raw.get("installed") or raw
    if not conf.get("client_id") or not conf.get("client_secret"):
        return "client_secret.json invalide : client_id/client_secret manquants."
    return {"client_id": conf["client_id"], "client_secret": conf["client_secret"]}


def _current_sub() -> str | None:
    claims = current_claims.get()
    return str(claims["sub"]) if claims and claims.get("sub") else None


# Access-token cache: sub -> (token, epoch expiry)
_token_cache: dict[str, tuple[str, float]] = {}


async def _access_token(sub: str) -> str | dict:
    """A live Google access token for `sub`, or an {'error': ...} dict."""
    cached = _token_cache.get(sub)
    if cached and time.time() < cached[1]:
        return cached[0]
    try:
        with open(_user_file(sub)) as f:
            user = json.load(f)
    except FileNotFoundError:
        return {"error": (
            f"aucun compte Google enrôlé pour « {sub} ». Ouvrir "
            f"{os.environ.get('ROSETTA_EXTERNAL_URL', 'https://rosetta.mcp.berard.me')}/google/enroll "
            "dans un navigateur pour autoriser l'accès (une seule fois)."
        )}
    client = _oauth_client()
    if isinstance(client, str):
        return {"error": client}
    async with _client() as http:
        r = await http.post(GOOGLE_TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": user["refresh_token"],
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
        })
        data = r.json()
    if r.status_code != 200:
        detail = data.get("error", f"HTTP {r.status_code}")
        if detail == "invalid_grant":
            return {"error": f"l'autorisation Google de « {sub} » a été révoquée ou a expiré : ré-enrôlement nécessaire (/google/enroll)."}
        return {"error": f"rafraîchissement du token Google impossible : {detail}"}
    token = data["access_token"]
    _token_cache[sub] = (token, time.time() + int(data.get("expires_in", 3600)) - 60)
    return token


async def _authed(sub_required: bool = True) -> tuple[str, dict] | dict:
    """(access_token, auth_headers) for the calling user, or {'error': ...}."""
    sub = _current_sub()
    if not sub:
        return {"error": "identité utilisateur absente du contexte d'appel (token machine ?)."}
    token = await _access_token(sub)
    if isinstance(token, dict):
        return token
    return token, {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------
# Gmail helpers
# --------------------------------------------------------------------------

def _header(payload: dict, name: str) -> str | None:
    for h in payload.get("headers") or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value")
    return None


def _b64url_decode(data: str) -> str:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad).decode("utf-8", "replace")


def _extract_body(payload: dict) -> str:
    """Best-effort readable text from a Gmail payload (text/plain first)."""
    if payload.get("mimeType", "").startswith("text/plain") and dig(payload, "body", "data"):
        return _b64url_decode(payload["body"]["data"])
    for part in payload.get("parts") or []:
        text = _extract_body(part)
        if text:
            return text
    if payload.get("mimeType", "").startswith("text/html") and dig(payload, "body", "data"):
        # Crude but dependency-free: strip tags from the html alternative.
        return re.sub(r"<[^>]+>", " ", _b64url_decode(payload["body"]["data"]))
    return ""


def _truncate(text: str, limit: int = 3000) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "\n[… tronqué]"


# --------------------------------------------------------------------------
# Tools - Gmail (lecture + brouillon, jamais d'envoi)
# --------------------------------------------------------------------------

@mcp.tool()
async def mail_search(query: str, max_results: int = 10) -> dict:
    """Recherche dans Gmail (syntaxe Gmail : from:, subject:, after:, is:unread…).

    query : la recherche (ex. « from:airbnb after:2026/07/01 »).
    max_results : nombre de messages (défaut 10, max 25).
    """
    auth = await _authed()
    if isinstance(auth, dict):
        return auth
    _, headers = auth
    max_results = max(1, min(int(max_results), 25))
    async with _client() as http:
        r = await http.get(f"{GMAIL}/messages", params={"q": query, "maxResults": max_results}, headers=headers)
        data = r.json()
        if r.status_code != 200:
            return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
        out = []
        for ref in data.get("messages") or []:
            m = await http.get(
                f"{GMAIL}/messages/{ref['id']}",
                params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
                headers=headers,
            )
            if m.status_code != 200:
                continue
            msg = m.json()
            payload = msg.get("payload") or {}
            out.append({
                "id": msg.get("id"),
                "thread_id": msg.get("threadId"),
                "from": _header(payload, "From"),
                "subject": _header(payload, "Subject"),
                "date": _header(payload, "Date"),
                "snippet": msg.get("snippet"),
            })
    return {"query": query, "messages": out}


@mcp.tool()
async def mail_thread(thread_id: str) -> dict:
    """Lit un fil Gmail complet, rendu lisible (expéditeur, date, texte de chaque message)."""
    auth = await _authed()
    if isinstance(auth, dict):
        return auth
    _, headers = auth
    async with _client() as http:
        r = await http.get(f"{GMAIL}/threads/{thread_id}", params={"format": "full"}, headers=headers)
        data = r.json()
    if r.status_code != 200:
        return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
    messages = []
    subject = None
    for msg in data.get("messages") or []:
        payload = msg.get("payload") or {}
        subject = subject or _header(payload, "Subject")
        messages.append({
            "from": _header(payload, "From"),
            "to": _header(payload, "To"),
            "date": _header(payload, "Date"),
            "body": _truncate(_extract_body(payload) or msg.get("snippet", "")),
        })
    return {"thread_id": thread_id, "subject": subject, "messages": messages}


@mcp.tool()
async def mail_draft(to: str, subject: str, body: str, thread_id: str | None = None) -> dict:
    """Dépose un BROUILLON dans Gmail — jamais d'envoi (c'est l'utilisateur qui clique).

    to : destinataire(s), séparés par des virgules.
    subject : objet (mettre « Re: … » pour une réponse).
    body : corps du message, texte brut.
    thread_id : optionnel, pour rattacher le brouillon à un fil existant.
    """
    auth = await _authed()
    if isinstance(auth, dict):
        return auth
    _, headers = auth
    mime = EmailMessage()
    mime["To"] = to
    mime["Subject"] = subject
    mime.set_content(body)
    message: dict = {}
    async with _client() as http:
        if thread_id:
            r = await http.get(
                f"{GMAIL}/threads/{thread_id}",
                params={"format": "metadata", "metadataHeaders": ["Message-ID"]},
                headers=headers,
            )
            if r.status_code == 200:
                msgs = r.json().get("messages") or []
                last_mid = _header((msgs[-1].get("payload") or {}), "Message-ID") if msgs else None
                if last_mid:
                    mime["In-Reply-To"] = last_mid
                    mime["References"] = last_mid
                message["threadId"] = thread_id
        message["raw"] = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        r = await http.post(f"{GMAIL}/drafts", json={"message": message}, headers=headers)
        data = r.json()
    if r.status_code != 200:
        return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
    return {"draft_id": data.get("id"), "status": "brouillon déposé dans Gmail — à relire et envoyer par l'utilisateur"}


# --------------------------------------------------------------------------
# Tools - Calendar (lecture + création/modification, pas de suppression)
# --------------------------------------------------------------------------

def _when(value: str) -> dict:
    """ISO date (all-day) or datetime -> Calendar API start/end object."""
    if len(value) == 10:
        return {"date": value}
    return {"dateTime": value, "timeZone": os.environ.get("TZ", DEFAULT_TZ)}


@mcp.tool()
async def calendar_events(time_min: str, time_max: str, max_results: int = 25) -> dict:
    """Liste les événements de l'agenda principal entre deux instants.

    time_min / time_max : ISO 8601 (ex. 2026-07-21T00:00:00+02:00).
    """
    auth = await _authed()
    if isinstance(auth, dict):
        return auth
    _, headers = auth
    async with _client() as http:
        r = await http.get(
            f"{CALENDAR}/calendars/primary/events",
            params={
                "timeMin": time_min, "timeMax": time_max, "singleEvents": "true",
                "orderBy": "startTime", "maxResults": max(1, min(int(max_results), 100)),
            },
            headers=headers,
        )
        data = r.json()
    if r.status_code != 200:
        return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
    out = []
    for ev in data.get("items") or []:
        out.append({
            "id": ev.get("id"),
            "summary": ev.get("summary"),
            "start": dig(ev, "start", "dateTime", default=dig(ev, "start", "date")),
            "end": dig(ev, "end", "dateTime", default=dig(ev, "end", "date")),
            "location": ev.get("location"),
        })
    return {"events": out}


@mcp.tool()
async def calendar_create(summary: str, start: str, end: str,
                          description: str | None = None, location: str | None = None) -> dict:
    """Crée un événement dans l'agenda principal (sur demande explicite de l'utilisateur).

    start / end : ISO 8601 (datetime), ou date seule YYYY-MM-DD pour du journée entière.
    """
    auth = await _authed()
    if isinstance(auth, dict):
        return auth
    _, headers = auth
    event = {"summary": summary, "start": _when(start), "end": _when(end)}
    if description:
        event["description"] = description
    if location:
        event["location"] = location
    async with _client() as http:
        r = await http.post(f"{CALENDAR}/calendars/primary/events", json=event, headers=headers)
        data = r.json()
    if r.status_code != 200:
        return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
    return {"id": data.get("id"), "status": "événement créé", "link": data.get("htmlLink")}


@mcp.tool()
async def calendar_update(event_id: str, summary: str | None = None, start: str | None = None,
                          end: str | None = None, description: str | None = None,
                          location: str | None = None) -> dict:
    """Modifie un événement existant (déplacement, renommage…) — confirmation utilisateur requise en amont."""
    auth = await _authed()
    if isinstance(auth, dict):
        return auth
    _, headers = auth
    patch: dict = {}
    if summary:
        patch["summary"] = summary
    if start:
        patch["start"] = _when(start)
    if end:
        patch["end"] = _when(end)
    if description is not None:
        patch["description"] = description
    if location is not None:
        patch["location"] = location
    if not patch:
        return {"error": "rien à modifier : aucun champ fourni."}
    async with _client() as http:
        r = await http.patch(f"{CALENDAR}/calendars/primary/events/{event_id}", json=patch, headers=headers)
        data = r.json()
    if r.status_code != 200:
        return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
    return {"id": data.get("id"), "status": "événement modifié", "link": data.get("htmlLink")}


# --------------------------------------------------------------------------
# Enrolment (browser flow, guarded by the ingress forwardAuth)
# --------------------------------------------------------------------------

def _state_key() -> bytes:
    path = os.path.join(_data_dir(), "state.key")
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        os.makedirs(_data_dir(), exist_ok=True)
        with open(path, "wb") as f:
            f.write(key)
        os.chmod(path, 0o600)
        return key


def _sign_state(sub: str) -> str:
    payload = f"{int(time.time()) + 600}.{sub}"
    sig = hmac.new(_state_key(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()


def _verify_state(state: str) -> str | None:
    try:
        expiry, sub, sig = base64.urlsafe_b64decode(state.encode()).decode().split(".", 2)
        payload = f"{expiry}.{sub}"
        if not hmac.compare_digest(sig, hmac.new(_state_key(), payload.encode(), hashlib.sha256).hexdigest()):
            return None
        if time.time() > int(expiry):
            return None
        return sub
    except Exception:
        return None


def _remote_user(request) -> str | None:
    # Set by the Authelia forwardAuth in front of these paths (ingress-level).
    return request.headers.get("Remote-User")


async def enroll(request):
    sub = _remote_user(request)
    if not sub:
        return HTMLResponse("<p>Accès direct refusé : cette page passe par le SSO.</p>", status_code=403)
    client = _oauth_client()
    if isinstance(client, str):
        return HTMLResponse(f"<p>{client}</p>", status_code=500)
    external = os.environ.get("ROSETTA_EXTERNAL_URL", "https://rosetta.mcp.berard.me").rstrip("/")
    params = {
        "client_id": client["client_id"],
        "redirect_uri": f"{external}/google/callback",
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": _sign_state(sub),
    }
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=302)


async def callback(request):
    state = request.query_params.get("state", "")
    code = request.query_params.get("code")
    sub = _verify_state(state)
    if not sub or not code:
        return HTMLResponse("<p>Flux invalide ou expiré — reprendre depuis /google/enroll.</p>", status_code=400)
    client = _oauth_client()
    if isinstance(client, str):
        return HTMLResponse(f"<p>{client}</p>", status_code=500)
    external = os.environ.get("ROSETTA_EXTERNAL_URL", "https://rosetta.mcp.berard.me").rstrip("/")
    async with _client() as http:
        r = await http.post(GOOGLE_TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "redirect_uri": f"{external}/google/callback",
        })
        data = r.json()
    if r.status_code != 200 or not data.get("refresh_token"):
        detail = data.get("error", f"HTTP {r.status_code}") if isinstance(data, dict) else r.status_code
        return HTMLResponse(f"<p>Échange Google refusé ({detail}).</p>", status_code=502)
    os.makedirs(os.path.join(_data_dir(), "users"), exist_ok=True)
    path = _user_file(sub)
    with open(path, "w") as f:
        json.dump({
            "sub": sub,
            "refresh_token": data["refresh_token"],
            "scopes": data.get("scope", "").split(),
            "enrolled_at": int(time.time()),
        }, f)
    os.chmod(path, 0o600)
    _token_cache.pop(sub, None)
    return HTMLResponse(f"<p>Compte Google enrôlé pour <b>{sub}</b>. Cette page peut être fermée.</p>")


extra_routes = [("/enroll", enroll, ["GET"]), ("/callback", callback, ["GET"])]
open_paths = ["/enroll", "/callback"]


if __name__ == "__main__":
    # Local stdio debugging: `python -m rosetta.addons.google`.
    mcp.run()
