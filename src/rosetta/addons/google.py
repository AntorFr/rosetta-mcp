"""`google` addon - Gmail + Calendar for the household agents, user-data class.

Contract (the guard IS the tool surface - deliberately narrow):
  - mail_search / mail_thread / mail_attachment : read-only Gmail
  - mail_drafts / mail_draft / mail_draft_update : list, read, create and amend
    DRAFTS - never sends, never deletes: no such tool exists
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
import unicodedata
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
GMAIL_WEB = "https://mail.google.com/mail"
CALENDAR = "https://www.googleapis.com/calendar/v3"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

# gmail.compose is the narrowest scope that allows drafts to be created AND
# amended (drafts.update lives behind the same scope - no re-enrolment was needed
# to gain the amendment tools). It nominally permits sending and draft deletion
# too - the guarantee that no mail ever leaves, and that nothing is destroyed, is
# that NO send and NO delete tool exists in this module, and the credentials never
# leave the server.
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
    if not claims:
        return None
    # Authelia access tokens may carry an opaque `sub`; the credential store is
    # keyed on the username - `preferred_username` when the profile scope was
    # granted (same value as the Remote-User header used at enrolment). NFC to
    # match the enrolment normalization.
    value = claims.get("preferred_username") or claims.get("sub")
    return unicodedata.normalize("NFC", str(value)) if value else None


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


# Attachment rendering budgets. Text is transcribed server-side and truncated;
# raw retrieval hands back the bytes (base64) once, so it is capped hard.
ATTACHMENT_TEXT_LIMIT = 20000
ATTACHMENT_RAW_MAX = 10 * 1024 * 1024
_TEXT_MIMES = {"application/json", "application/xml"}

# Display cap when re-reading a draft. Generous: a draft is our own short prose,
# and a body clipped on screen must never be mistaken for the body on file.
DRAFT_BODY_LIMIT = 20000


def _list_attachments(payload: dict) -> list[dict]:
    """Inventory of real attachments (a part with a filename and an attachmentId),
    walking the MIME tree depth-first."""
    found: list[dict] = []

    def walk(part: dict) -> None:
        body = part.get("body") or {}
        if part.get("filename") and body.get("attachmentId"):
            found.append({
                "attachment_id": body["attachmentId"],
                "filename": part.get("filename"),
                "mime_type": part.get("mimeType"),
                "size_bytes": body.get("size"),
            })
        for sub in part.get("parts") or []:
            walk(sub)

    walk(payload)
    return found


# Mailbox address cache: sub -> address (or "0"). Only used to build web links.
_mailbox_cache: dict[str, str] = {}


async def _mailbox(http, headers: dict) -> str:
    """The account segment of a Gmail web URL for the calling user.

    Gmail addresses a mailbox by its index (/mail/u/0/) OR by its address
    (/mail/u/someone@example.com/), the latter resolving to the right index
    whichever accounts are signed in - which is what we want, since we cannot know
    the browser's account order. Falls back to index 0 if the profile is
    unreadable. Cached: the address never changes for a given subject.
    """
    sub = _current_sub() or ""
    if sub not in _mailbox_cache:
        try:
            r = await http.get(f"{GMAIL}/profile", headers=headers)
            address = r.json().get("emailAddress") if r.status_code == 200 else None
        except Exception:
            address = None
        _mailbox_cache[sub] = address or "0"
    return _mailbox_cache[sub]


def _draft_link(mailbox: str, message_id: str | None) -> str | None:
    """Deep link to a draft in the Gmail web UI.

    The fragment carries the DRAFT'S MESSAGE id (hex, e.g. 19faa841267fcac6), not
    the draft id (`r-84…`) - opening the latter yields nothing. Note the message id
    is reminted on every amendment, so a link taken before an update goes stale.
    """
    return f"{GMAIL_WEB}/u/{mailbox}/#drafts/{message_id}" if message_id else None


def _build_mime(to: str, subject: str, body: str, in_reply_to: str | None = None,
                references: str | None = None) -> str:
    """A plain-text draft, base64url-encoded for the Gmail `raw` field."""
    mime = EmailMessage()
    mime["To"] = to
    mime["Subject"] = subject
    if in_reply_to:
        mime["In-Reply-To"] = in_reply_to
        mime["References"] = references or in_reply_to
    mime.set_content(body)
    return base64.urlsafe_b64encode(mime.as_bytes()).decode()


def _draft_summary(draft: dict, mailbox: str) -> dict:
    """id, headers and web link of a draft, from a format=metadata (or full) drafts.get."""
    payload = dig(draft, "message", "payload", default={}) or {}
    return {
        "draft_id": draft.get("id"),
        "thread_id": dig(draft, "message", "threadId"),
        "to": _header(payload, "To"),
        "subject": _header(payload, "Subject"),
        "date": _header(payload, "Date"),
        "link": _draft_link(mailbox, dig(draft, "message", "id")),
    }


def _pdf_to_text(raw: bytes) -> str:
    """Extract text from a PDF's bytes. Lazy import: pypdf is only pulled when a
    PDF is actually opened, and it is trivial to stub in tests."""
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


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
async def mail_attachment(message_id: str, attachment_id: str | None = None,
                          raw: bool = False) -> dict:
    """Lit ou rapatrie les pièces jointes d'un mail Gmail.

    - Sans `attachment_id` : liste les pièces jointes du message (nom, type, taille, id).
    - Avec `attachment_id` (défaut) : rend la pièce en TEXTE quand c'est possible
      (texte brut, CSV/JSON, PDF) — pour la LIRE. Un binaire opaque (image, archive)
      ne renvoie que ses métadonnées, jamais les octets bruts.
    - Avec `raw=True` : rapatrie la pièce au FORMAT NATIF (octets en base64), pour la
      STOCKER telle quelle (mémoire, pièce jointe d'une fiche). Plafonné en taille.

    message_id : l'id du message (donné par mail_search).
    attachment_id : l'id de la pièce (donné par ce même outil en mode liste).
    raw : True pour récupérer les octets bruts au lieu d'une transcription texte.
    """
    auth = await _authed()
    if isinstance(auth, dict):
        return auth
    _, headers = auth

    async with _client() as http:
        meta = await http.get(f"{GMAIL}/messages/{message_id}",
                              params={"format": "full"}, headers=headers)
        meta_data = meta.json()
        if meta.status_code != 200:
            return {"error": dig(meta_data, "error", "message", default=f"HTTP {meta.status_code}")}
        attachments = _list_attachments(meta_data.get("payload") or {})

        if not attachment_id:
            return {"message_id": message_id, "attachments": attachments}

        info = next((a for a in attachments if a["attachment_id"] == attachment_id), None)
        r = await http.get(
            f"{GMAIL}/messages/{message_id}/attachments/{attachment_id}", headers=headers)
        data = r.json()
    if r.status_code != 200:
        return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}

    content = base64.urlsafe_b64decode(data["data"] + "=" * (-len(data["data"]) % 4))
    mime = (info or {}).get("mime_type") or ""
    filename = (info or {}).get("filename")
    out: dict = {"message_id": message_id, "attachment_id": attachment_id,
                 "filename": filename, "mime_type": mime, "size_bytes": len(content)}

    # Native retrieval: hand back the bytes (base64) so the caller can persist the
    # file as-is. Capped: the blob crosses the agent context once, so refuse to
    # inline anything oversized rather than blow the window.
    if raw:
        if len(content) > ATTACHMENT_RAW_MAX:
            out["note"] = (f"trop volumineux pour un rapatriement inline "
                           f"({len(content)} octets > {ATTACHMENT_RAW_MAX}).")
            return out
        out["encoding"] = "base64"
        out["data_base64"] = base64.b64encode(content).decode()
        return out

    # Reading: transcribe to text server-side; only the text crosses to the agent.
    # Gmail's declared MIME is unreliable — senders routinely mislabel attachments
    # as application/octet-stream, or drop the .pdf extension. So we SNIFF the bytes:
    # the magic number is ground truth, the label is only a hint.
    head = content[:1024]
    looks_pdf = (mime == "application/pdf" or (filename or "").lower().endswith(".pdf")
                 or b"%PDF-" in head)
    looks_text = mime.startswith("text/") or mime in _TEXT_MIMES

    if looks_pdf:
        try:
            text = _pdf_to_text(content)
        except Exception as exc:  # encrypted, corrupt, or not really a PDF
            out["note"] = f"PDF illisible (chiffré ou corrompu ?) : {exc}"
            return out
        out["text"] = _truncate(text, ATTACHMENT_TEXT_LIMIT) or \
            "[PDF sans texte extractible — probablement un scan image ; utiliser raw=True pour le stocker]"
    elif looks_text:
        out["text"] = _truncate(content.decode("utf-8", "replace"), ATTACHMENT_TEXT_LIMIT)
    else:
        out["note"] = (f"type non transcrit (déclaré : {mime or 'inconnu'}, fichier : "
                       f"{filename or 'sans nom'}) — {len(content)} octets ; "
                       f"utiliser raw=True pour le rapatrier au format natif.")
    return out


@mcp.tool()
async def mail_draft(to: str, subject: str, body: str, thread_id: str | None = None) -> dict:
    """Dépose un BROUILLON dans Gmail — jamais d'envoi (c'est l'utilisateur qui clique).

    to : destinataire(s), séparés par des virgules.
    subject : objet (mettre « Re: … » pour une réponse).
    body : corps du message, texte brut.
    thread_id : optionnel, pour rattacher le brouillon à un fil existant.

    Rend `draft_id` (pour corriger ensuite via mail_draft_update) et `link`, l'URL
    du brouillon dans Gmail — à donner telle quelle à l'utilisateur pour qu'il
    l'ouvre, le relise et l'envoie.
    """
    auth = await _authed()
    if isinstance(auth, dict):
        return auth
    _, headers = auth
    message: dict = {}
    last_mid = None
    async with _client() as http:
        if thread_id:
            # Gmail nests a draft in a thread ONLY if threadId is set on the
            # draft's message RESOURCE - headers alone leave it orphaned. Set it
            # unconditionally; the metadata fetch below only serves the
            # In-Reply-To/References headers and stays best-effort.
            message["threadId"] = thread_id
            r = await http.get(
                f"{GMAIL}/threads/{thread_id}",
                params={"format": "metadata", "metadataHeaders": ["Message-ID"]},
                headers=headers,
            )
            if r.status_code == 200:
                msgs = r.json().get("messages") or []
                last_mid = _header((msgs[-1].get("payload") or {}), "Message-ID") if msgs else None
        message["raw"] = _build_mime(to, subject, body, in_reply_to=last_mid)
        r = await http.post(f"{GMAIL}/drafts", json={"message": message}, headers=headers)
        data = r.json()
        if r.status_code != 200:
            return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
        mailbox = await _mailbox(http, headers)
    return {"draft_id": data.get("id"),
            "link": _draft_link(mailbox, dig(data, "message", "id")),
            "status": "brouillon déposé dans Gmail — à relire et envoyer par l'utilisateur"}


@mcp.tool()
async def mail_drafts(draft_id: str | None = None, max_results: int = 10) -> dict:
    """Liste les brouillons Gmail, ou relit l'un d'eux en entier.

    - Sans `draft_id` : liste les brouillons en attente (id, destinataire, objet, date).
      C'est ainsi qu'on RETROUVE un brouillon déposé lors d'une session précédente.
    - Avec `draft_id` : rend le brouillon complet (destinataire, objet, corps), pour le
      relire avant de le corriger avec mail_draft_update.

    max_results : nombre de brouillons listés (défaut 10, max 25).
    """
    auth = await _authed()
    if isinstance(auth, dict):
        return auth
    _, headers = auth

    async with _client() as http:
        if draft_id:
            r = await http.get(f"{GMAIL}/drafts/{draft_id}", params={"format": "full"},
                               headers=headers)
            data = r.json()
            if r.status_code != 200:
                return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
            payload = dig(data, "message", "payload", default={}) or {}
            out = _draft_summary(data, await _mailbox(http, headers))
            out["body"] = _truncate(_extract_body(payload), DRAFT_BODY_LIMIT)
            return out

        max_results = max(1, min(int(max_results), 25))
        r = await http.get(f"{GMAIL}/drafts", params={"maxResults": max_results},
                           headers=headers)
        data = r.json()
        if r.status_code != 200:
            return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
        # drafts.list only yields ids: the headers need one metadata fetch each
        # (same shape as mail_search).
        mailbox = await _mailbox(http, headers)
        out = []
        for ref in data.get("drafts") or []:
            d = await http.get(f"{GMAIL}/drafts/{ref['id']}", params={"format": "metadata"},
                               headers=headers)
            if d.status_code != 200:
                continue
            out.append(_draft_summary(d.json(), mailbox))
    return {"drafts": out}


@mcp.tool()
async def mail_draft_update(draft_id: str, to: str | None = None, subject: str | None = None,
                            body: str | None = None) -> dict:
    """Corrige un BROUILLON existant — toujours pas d'envoi, et rien n'est supprimé.

    Seuls les champs fournis changent : le reste du brouillon (destinataire, objet,
    corps, rattachement au fil) est conservé tel quel. Pour retrouver le `draft_id`
    ou relire ce qu'il contient avant de corriger, passer par mail_drafts.

    draft_id : l'id du brouillon (rendu par mail_draft ou mail_drafts).
    to : nouveau(x) destinataire(s), séparés par des virgules.
    subject : nouvel objet.
    body : nouveau corps, texte brut (REMPLACE l'ancien, ne s'y ajoute pas).
    """
    if to is None and subject is None and body is None:
        return {"error": "rien à modifier : aucun champ fourni."}
    auth = await _authed()
    if isinstance(auth, dict):
        return auth
    _, headers = auth

    async with _client() as http:
        # drafts.update REPLACES the whole draft: whatever is not re-sent is lost.
        # So read the current one first and merge - in particular threadId, without
        # which the amended draft would silently fall out of its thread.
        r = await http.get(f"{GMAIL}/drafts/{draft_id}", params={"format": "full"},
                           headers=headers)
        data = r.json()
        if r.status_code != 200:
            return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
        payload = dig(data, "message", "payload", default={}) or {}
        message: dict = {"raw": _build_mime(
            # Untruncated on purpose: this text is written back to Gmail, so the
            # display cap of mail_drafts must never reach it.
            to if to is not None else (_header(payload, "To") or ""),
            subject if subject is not None else (_header(payload, "Subject") or ""),
            body if body is not None else _extract_body(payload),
            in_reply_to=_header(payload, "In-Reply-To"),
            references=_header(payload, "References"),
        )}
        thread_id = dig(data, "message", "threadId")
        if thread_id:
            message["threadId"] = thread_id
        r = await http.put(f"{GMAIL}/drafts/{draft_id}", json={"message": message},
                           headers=headers)
        data = r.json()
        if r.status_code != 200:
            return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
        mailbox = await _mailbox(http, headers)
    return {"draft_id": data.get("id") or draft_id,
            # Reminted by the amendment: any link handed out earlier is now stale.
            "link": _draft_link(mailbox, dig(data, "message", "id")),
            "status": "brouillon corrigé dans Gmail — à relire et envoyer par l'utilisateur"}


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


def _page(glyph: str, title: str, message: str, status: int = 200) -> HTMLResponse:
    """Minimal self-contained page for the browser-facing enrolment flow."""
    return HTMLResponse(f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>rosetta — Google</title><style>
 body{{margin:0;min-height:100vh;display:grid;place-items:center;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  background:#f4f1ea;color:#2b2b2b}}
 .card{{max-width:26rem;margin:1rem;padding:2.4rem 2.6rem;border-radius:16px;
  background:#fff;box-shadow:0 10px 34px rgba(0,0,0,.09);text-align:center}}
 .glyph{{font-size:2.6rem;line-height:1}}
 h1{{font-size:.82rem;letter-spacing:.22em;text-transform:uppercase;
  opacity:.5;margin:1rem 0 .4rem}}
 h2{{font-size:1.15rem;margin:.2rem 0 .8rem}}
 p{{line-height:1.55;margin:0;opacity:.85}}
 @media (prefers-color-scheme:dark){{
  body{{background:#171614;color:#eae6df}}
  .card{{background:#232019;box-shadow:0 10px 34px rgba(0,0,0,.55)}}}}
</style></head><body><div class="card">
<div class="glyph">{glyph}</div><h1>Rosetta · Google</h1>
<h2>{title}</h2><p>{message}</p>
</div></body></html>""", status_code=status)


def _remote_user(request) -> str | None:
    # Set by the Authelia forwardAuth in front of these paths (ingress-level).
    value = request.headers.get("Remote-User")
    if value is None:
        return None
    # HTTP headers are latin-1 on the wire but Authelia emits UTF-8 bytes:
    # recover accents ("SÃ©bastien" -> "Sébastien"), then normalize (NFC) so
    # the credential key is stable across clients and keyboards.
    try:
        value = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return unicodedata.normalize("NFC", value)


async def enroll(request):
    sub = _remote_user(request)
    if not sub:
        return _page("🚪", "Accès refusé", "Cette page passe par le SSO de la maison — pas par la porte de service.", 403)
    client = _oauth_client()
    if isinstance(client, str):
        return _page("🧩", "Configuration absente", client, 500)
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
        return _page("⏳", "Flux invalide ou expiré", "Reprendre depuis /google/enroll — le lien n'est valable que dix minutes.", 400)
    client = _oauth_client()
    if isinstance(client, str):
        return _page("🧩", "Configuration absente", client, 500)
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
        return _page("🛑", "Échange refusé par Google", f"Détail : {detail}. Reprendre depuis /google/enroll.", 502)
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
    return _page("🔏", "Compte enrôlé",
                 f"Le compte Google de <b>{sub}</b> est désormais au service de la maison. "
                 "Cette page peut être fermée.")


extra_routes = [("/enroll", enroll, ["GET"]), ("/callback", callback, ["GET"])]
open_paths = ["/enroll", "/callback"]


if __name__ == "__main__":
    # Local stdio debugging: `python -m rosetta.addons.google`.
    mcp.run()
