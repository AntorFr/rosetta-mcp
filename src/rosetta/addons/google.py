"""`google` addon - Gmail + Calendar for the household agents, user-data class.

Contract (the guard IS the tool surface - deliberately narrow):
  - mail_search / mail_thread / mail_attachment : read-only Gmail
  - mail_drafts / mail_draft / mail_draft_update : list, read, create and amend
    DRAFTS - never sends, never deletes: no such tool exists
  - calendar_list : the account's calendars, so a caller can CHOOSE where it
    reads and where it writes - every other calendar tool takes a `calendar_id`
    (default "primary"), and every event read carries the calendar it came from
  - calendar_events / calendar_create / calendar_update : no delete tool exists,
    and no move-between-calendars tool either
  - attendees ARE writable (0.23.0), and `send_updates` decides who gets an
    invitation MAIL (default: the guests without a Google Calendar, who would
    otherwise be invited to nothing). This is the addon's ONLY outbound channel -
    everywhere else "no send" is structural, guaranteed by the absence of a tool.
    Recipient and text are both caller-chosen, so an invitation is by nature an
    exfiltration path. Nothing here can close it: WHO may be invited is contextual
    policy and belongs to the calling agent's guard (channel, human confirmation,
    allowlist) - the hub knows neither channel nor shield.

Identity: `identity = "user"` - the hub refuses machine tokens on /google, so
every call carries a human `sub` (Authelia). Google credentials are stored
SERVER-SIDE, one file per subject under ROSETTA_GOOGLE_DATA; agents never see
them. Enrolment is a one-time browser flow (/google/enroll -> Google consent ->
/google/callback), guarded by the ingress forwardAuth (Remote-User header),
which yields the per-user Google refresh token.

Tool descriptions are in French - runtime UX for the household agents.
"""

import asyncio
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
from urllib.parse import quote, urlencode

import httpx
from starlette.responses import HTMLResponse, RedirectResponse

from ..auth import current_claims
from ._common import TIMEOUT, dig, enrol_page, new_server, remote_user

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
    # Read-only listing of the account's calendars - `calendar.events` grants event
    # read/write on ALL calendars but never the LIST of them (verified against the
    # calendarList.list reference). Added 0.23.0: an enrolment older than that lacks
    # it, hence the explicit re-enrolment message rather than an opaque 403.
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
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
            f"{os.environ.get('ROSETTA_EXTERNAL_URL', '')}/google/enroll "
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


def _truncate(text: str, limit: int = 3000, *, param: str = "") -> str:
    """Clip, and say so WITH NUMBERS - a silent cut is a data loss disguised as
    an answer. The marker carries the sizes and, when the caller can lift the
    cap, the exact parameter to raise: an agent that hits it must be able to
    recover the tail without guessing the threshold (it did guess, in August
    2026, and reported an outage instead of a truncation)."""
    text = text.strip()
    if len(text) <= limit:
        return text
    hint = f", rappeler avec {param}=<n> pour la suite" if param else ""
    return (text[:limit]
            + f"\n[… tronqué : {limit} caractères rendus sur {len(text)}{hint}]")


# Attachment rendering budgets. Text is transcribed server-side and truncated;
# raw retrieval hands back the bytes (base64) once, so it is capped hard.
# mail_thread budgets. Per-message default stays modest because a thread returns
# N bodies at once; but it is a PARAMETER, and the truncation marker names it -
# a cap the caller cannot lift and cannot even measure is how a booking mail lost
# its October legs in August 2026. The total budget keeps a long thread from
# blowing the caller's context when body_limit is raised.
THREAD_BODY_LIMIT = 4000
THREAD_BODY_LIMIT_MAX = 40000
THREAD_TOTAL_BUDGET = 120000

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


def _draft_link(thread_id: str | None) -> str | None:
    """Deep link to a draft in the Gmail web UI. Verified against the real UI.

    Two things were wrong in 0.4.x and are settled here by testing, not reasoning:

    - The ACCOUNT segment must be the index (/mail/u/0/). The address form
      (/mail/u/someone@example.com/) reads plausibly and 404s. Overridable via
      ROSETTA_GMAIL_ACCOUNT for a mailbox that is not the browser's first account.
    - The FRAGMENT must be the THREAD id. The draft's message id also opens the
      draft, but Gmail remints it on every amendment, so any link handed out
      earlier goes dead. The thread id survives edits. A brand-new draft hides the
      bug: Gmail gives its first message an id equal to the thread id, so building
      on either looked identical until the draft was amended.

    Never the draft id (`r-84…`) - that one opens nothing at all.
    """
    account = os.environ.get("ROSETTA_GMAIL_ACCOUNT", "0")
    return f"{GMAIL_WEB}/u/{account}/#drafts/{thread_id}" if thread_id else None


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


def _draft_summary(draft: dict) -> dict:
    """id, headers and web link of a draft, from a format=metadata (or full) drafts.get."""
    payload = dig(draft, "message", "payload", default={}) or {}
    thread_id = dig(draft, "message", "threadId")
    return {
        "draft_id": draft.get("id"),
        "thread_id": thread_id,
        "to": _header(payload, "To"),
        "subject": _header(payload, "Subject"),
        "date": _header(payload, "Date"),
        "link": _draft_link(thread_id),
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
    """Recherche dans GMAIL (syntaxe Gmail : from:, subject:, after:, is:unread…).

    ⚠️ Gmail, PAS la boîte personnelle @<domaine familial> : celle-là est
    l'addon `courrier` (`courrier_recherche`). Les deux existent et ne
    contiennent pas les mêmes messages. Si Monsieur dit juste « mes mails »
    sans préciser, lui demander laquelle plutôt que d'en choisir une.


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
async def mail_thread(thread_id: str, body_limit: int = THREAD_BODY_LIMIT) -> dict:
    """Lit un fil GMAIL complet, rendu lisible (expéditeur, date, texte de chaque message).

    ⚠️ Gmail, PAS la boîte @<domaine familial> : celle-là est `courrier_lire`.

    body_limit : caractères rendus PAR MESSAGE (défaut 4000, max 40000). Un corps
    coupé le dit avec ses tailles réelles — si un message est tronqué et que sa
    fin compte (une réservation groupée, un long récapitulatif), rappeler cet
    outil avec un `body_limit` plus grand plutôt que de conclure sur un texte
    incomplet. Un fil entier reste borné à THREAD_TOTAL_BUDGET caractères : les
    messages au-delà sont réduits, et le fil le signale dans `tronque`.
    """
    auth = await _authed()
    if isinstance(auth, dict):
        return auth
    _, headers = auth
    async with _client() as http:
        r = await http.get(f"{GMAIL}/threads/{thread_id}", params={"format": "full"}, headers=headers)
        data = r.json()
    if r.status_code != 200:
        return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
    # Clamp rather than reject: an agent asking for 10_000_000 gets the ceiling
    # and a note, not an error it would have to understand before retrying.
    per_msg = max(500, min(int(body_limit or THREAD_BODY_LIMIT), THREAD_BODY_LIMIT_MAX))
    messages = []
    subject = None
    spent = 0
    clipped_by_budget = 0
    for msg in data.get("messages") or []:
        payload = msg.get("payload") or {}
        subject = subject or _header(payload, "Subject")
        # The per-message cap is the caller's; the total budget is the hard one.
        # Later messages shrink rather than the whole call failing - a long thread
        # must never be the reason a short answer becomes unreadable.
        room = max(500, min(per_msg, THREAD_TOTAL_BUDGET - spent))
        if room < per_msg:
            clipped_by_budget += 1
        body = _truncate(_extract_body(payload) or msg.get("snippet", ""),
                         room, param="body_limit")
        spent += len(body)
        entry = {
            "id": msg.get("id"),          # feeds mail_draft(reply_to_message_id=…)
            "from": _header(payload, "From"),
            "to": _header(payload, "To"),
            "date": _header(payload, "Date"),
            "body": body,
        }
        # Only when the sender asked to be answered elsewhere - the case that makes
        # a reply to `from` land nowhere. Free: format=full already carried it.
        reply_to = _header(payload, "Reply-To")
        if reply_to:
            entry["reply_to"] = reply_to
        messages.append(entry)
    out = {"thread_id": thread_id, "subject": subject,
           "body_limit": per_msg, "messages": messages}
    if clipped_by_budget:
        out["tronque"] = (
            f"{clipped_by_budget} message(s) réduits sous le budget total du fil "
            f"({THREAD_TOTAL_BUDGET} caractères). Pour lire l'un d'eux en entier, "
            "le demander seul via son `id`.")
    return out


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
async def mail_draft(body: str, to: str | None = None, subject: str | None = None,
                     thread_id: str | None = None,
                     reply_to_message_id: str | None = None) -> dict:
    """Dépose un BROUILLON dans GMAIL — jamais d'envoi (c'est l'utilisateur qui clique).

    ⚠️ Dans Gmail, PAS dans la boîte @<domaine familial> (celle-là :
    `courrier_brouillon`). Le brouillon n'apparaîtra que dans le client de
    CETTE boîte : se tromper d'addon, c'est écrire une réponse que le
    destinataire du fil d'origine ne verra jamais.


    POUR RÉPONDRE À UN MAIL, ne renseigner que `reply_to_message_id` et `body` : le
    serveur dérive le fil, le destinataire et l'objet. C'est la voie à préférer, et
    pas seulement par confort — elle évite d'écrire au `From` quand l'expéditeur a
    posé un `Reply-To` (les plateformes envoient depuis une adresse automatique).
    Un brouillon adressé au mauvais destinataire a l'air parfaitement correct : on ne
    s'en aperçoit qu'une fois envoyé.

    body : corps du message, texte brut.
    to : destinataire(s), séparés par des virgules. Requis SAUF en réponse ; fourni
         en réponse, il écrase le destinataire dérivé.
    subject : objet. En réponse, dérivé du message parent (« Re: … ») s'il est omis.
    thread_id : rattache le brouillon à un fil (dérivé en réponse, inutile à donner).
    reply_to_message_id : l'id du message auquel on répond (rendu par mail_search).

    Rend `draft_id` (pour corriger ensuite via mail_draft_update), `to` et `subject`
    RÉELLEMENT utilisés — à vérifier avant d'annoncer quoi que ce soit — et `link`,
    l'URL du brouillon dans Gmail, à donner telle quelle à l'utilisateur.
    """
    auth = await _authed()
    if isinstance(auth, dict):
        return auth
    _, headers = auth
    message: dict = {}
    in_reply_to = references = None

    async with _client() as http:
        if reply_to_message_id:
            r = await http.get(
                f"{GMAIL}/messages/{reply_to_message_id}",
                params={"format": "metadata", "metadataHeaders":
                        ["Reply-To", "From", "Subject", "Message-ID", "References"]},
                headers=headers,
            )
            data = r.json()
            if r.status_code != 200:
                return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
            parent = data.get("payload") or {}
            thread_id = thread_id or data.get("threadId")
            # Reply-To wins over From: a sender posting from a no-reply address uses
            # it to say where to actually write back. Answering From lands in a void.
            to = to or _header(parent, "Reply-To") or _header(parent, "From")
            if subject is None:
                parent_subject = (_header(parent, "Subject") or "").strip()
                subject = (parent_subject if parent_subject.lower().startswith("re:")
                           else f"Re: {parent_subject}".strip())
            # Chain onto THIS message, and carry its References so the thread holds
            # in the recipient's client too, not just in our Gmail.
            in_reply_to = _header(parent, "Message-ID")
            references = " ".join(x for x in (_header(parent, "References"), in_reply_to) if x)

        if not to:
            return {"error": "destinataire absent : fournir `to`, ou `reply_to_message_id` "
                             "pour répondre à un message existant."}

        if thread_id:
            # Gmail nests a draft in a thread ONLY if threadId is set on the
            # draft's message RESOURCE - headers alone leave it orphaned. Set it
            # unconditionally; the metadata fetch below only serves the
            # In-Reply-To/References headers and stays best-effort.
            message["threadId"] = thread_id
            if not in_reply_to:
                # Thread given without a parent message: chain onto its last one.
                r = await http.get(
                    f"{GMAIL}/threads/{thread_id}",
                    params={"format": "metadata", "metadataHeaders": ["Message-ID"]},
                    headers=headers,
                )
                if r.status_code == 200:
                    msgs = r.json().get("messages") or []
                    in_reply_to = _header((msgs[-1].get("payload") or {}),
                                          "Message-ID") if msgs else None
        message["raw"] = _build_mime(to, subject or "", body,
                                     in_reply_to=in_reply_to, references=references)
        r = await http.post(f"{GMAIL}/drafts", json={"message": message}, headers=headers)
        data = r.json()
    if r.status_code != 200:
        return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
    return {"draft_id": data.get("id"), "to": to, "subject": subject or "",
            "link": _draft_link(dig(data, "message", "threadId")),
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
            out = _draft_summary(data)
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
        out = []
        for ref in data.get("drafts") or []:
            d = await http.get(f"{GMAIL}/drafts/{ref['id']}", params={"format": "metadata"},
                               headers=headers)
            if d.status_code != 200:
                continue
            out.append(_draft_summary(d.json()))
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

    Rend le MÊME `link` que le dépôt : il est bâti sur le fil, qui ne bouge pas.
    Un lien donné à l'utilisateur avant une correction reste donc valable.
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
    return {"draft_id": data.get("id") or draft_id,
            # The thread id read BEFORE the write, not anything the PUT hands back:
            # its response carries a transient message id that only lands on the
            # #drafts folder. This link is the very one the deposit returned.
            "link": _draft_link(thread_id),
            "status": "brouillon corrigé dans Gmail — à relire et envoyer par l'utilisateur"}


# --------------------------------------------------------------------------
# Tools - Calendar (lecture + création/modification, pas de suppression)
# --------------------------------------------------------------------------

def _when(value: str) -> dict:
    """ISO date (all-day) or datetime -> Calendar API start/end object."""
    if len(value) == 10:
        return {"date": value}
    return {"dateTime": value, "timeZone": os.environ.get("TZ", DEFAULT_TZ)}


def _cal_path(calendar_id: str) -> str:
    """Base URL of one calendar. Every id reaching a Calendar URL goes through
    here: an id is address-shaped, and the shared ones are worse -
    `fr.french#holiday@group.v.calendar.google.com` carries a `#`, which raw in a
    path ends it and opens a fragment. Quoting is not cosmetic."""
    return f"{CALENDAR}/calendars/{quote(calendar_id.strip(), safe='')}"


# Event `visibility`. `private` is the one that matters here: on a calendar shared
# with someone else, a private event shows as busy and its details stay hidden -
# `default` inherits the calendar's setting, which on a shared calendar usually
# means everything is readable. `confidential` is a legacy alias of `private`,
# accepted because Google still returns it on old events.
VISIBILITIES = {"default", "public", "private", "confidential"}

_EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")

# `calendar_id="*"` fans out one HTTP call per calendar: a Google account carries
# holiday and subscribed calendars nobody asked for, so the fan-out is bounded.
MAX_CALENDARS = 15

CALENDARLIST_SCOPE = "https://www.googleapis.com/auth/calendar.calendarlist.readonly"

_REENROL = (
    "l'autorisation Google en cours ne couvre pas la LISTE des agendas : elle est "
    "antérieure à cet outil. Ouvrir une fois {url}/google/enroll pour la renouveler "
    "(le reste de l'agenda continue de marcher sans ça)."
)


def _reenrol_error() -> dict:
    return {"error": _REENROL.format(url=os.environ.get("ROSETTA_EXTERNAL_URL", ""))}


def _enrolled_scopes() -> list[str]:
    """Scopes Google granted at the last enrolment, as stored. Empty when unknown -
    an unknown scope set is never treated as a missing one: we let the API answer."""
    sub = _current_sub()
    if not sub:
        return []
    try:
        with open(_user_file(sub)) as f:
            return json.load(f).get("scopes") or []
    except Exception:
        return []


def _items(value) -> list[str]:
    """A list, or one string holding several entries separated by , ; or spaces."""
    if value is None:
        return []
    items = [str(v) for v in value] if isinstance(value, (list, tuple)) \
        else re.split(r"[\s,;]+", str(value))
    return [i.strip() for i in items if i.strip()]


def _guests(value) -> list[dict] | dict:
    """Attendee emails -> Calendar API attendees, or an {'error': ...} dict.

    Deliberately strict: an entry that is not plainly an address is refused rather
    than passed on. A malformed attendee is not worth a silent 400 from Google, and
    guessing what `Jean <j@x.fr>` meant is how an event lands on a stranger's
    calendar."""
    emails = _items(value)
    bad = [e for e in emails if not _EMAIL_RE.match(e)]
    if bad:
        return {"error": "adresse(s) d'invité invalide(s) : %s — une adresse mail simple "
                         "par invité, sans nom ni chevrons." % ", ".join(bad)}
    seen, out = set(), []
    for e in emails:
        key = e.lower()
        if key not in seen:
            seen.add(key)
            out.append({"email": e})
    return out


def _visibility(value: str | None) -> str | dict | None:
    if value is None:
        return None
    v = str(value).strip().lower()
    if v not in VISIBILITIES:
        return {"error": "visibilité « %s » inconnue : %s." % (value, " / ".join(sorted(VISIBILITIES)))}
    return v


def _read_guests(ev: dict) -> list[dict]:
    out = []
    for a in ev.get("attendees") or []:
        who = {"email": a.get("email")}
        if a.get("displayName"):
            who["nom"] = a["displayName"]
        if a.get("responseStatus"):
            who["reponse"] = a["responseStatus"]
        if a.get("organizer"):
            who["organisateur"] = True
        out.append(who)
    return out


async def _calendar_ids(http, headers) -> list[dict] | dict:
    """The account's calendars, or an {'error': ...} dict."""
    r = await http.get(f"{CALENDAR}/users/me/calendarList",
                       params={"maxResults": 250, "showHidden": "false"}, headers=headers)
    data = r.json()
    if r.status_code in (401, 403):
        return _reenrol_error()
    if r.status_code != 200:
        return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
    out = []
    for cal in data.get("items") or []:
        role = cal.get("accessRole")
        entry = {
            "calendar_id": cal.get("id"),
            "nom": cal.get("summaryOverride") or cal.get("summary"),
            "acces": role,
            "peut_ecrire": role in ("owner", "writer"),
        }
        if cal.get("primary"):
            entry["principal"] = True
        if cal.get("timeZone"):
            entry["fuseau"] = cal["timeZone"]
        if cal.get("description"):
            entry["description"] = cal["description"]
        out.append(entry)
    return out


@mcp.tool()
async def calendar_list() -> dict:
    """Liste les agendas du compte : identifiant, nom, et si on peut y écrire.

    L'identifiant rendu ici (`calendar_id`) est celui à repasser aux autres outils
    d'agenda pour choisir OÙ lire et OÙ écrire. « primary » désigne toujours
    l'agenda principal, sans avoir à le lister.

    `peut_ecrire: false` = agenda en lecture seule (agenda partagé par un tiers,
    jours fériés, abonnement) : y créer un événement sera refusé par Google.
    """
    scopes = _enrolled_scopes()
    if scopes and CALENDARLIST_SCOPE not in scopes:
        return _reenrol_error()
    auth = await _authed()
    if isinstance(auth, dict):
        return auth
    _, headers = auth
    async with _client() as http:
        cals = await _calendar_ids(http, headers)
    if isinstance(cals, dict):
        return cals
    return {"agendas": cals}


async def _fetch_events(http, headers, cal_id, time_min, time_max, limit):
    r = await http.get(
        f"{_cal_path(cal_id)}/events",
        params={"timeMin": time_min, "timeMax": time_max, "singleEvents": "true",
                "orderBy": "startTime", "maxResults": limit},
        headers=headers,
    )
    data = r.json()
    if r.status_code != 200:
        return cal_id, [], dig(data, "error", "message", default=f"HTTP {r.status_code}")
    out = []
    for ev in data.get("items") or []:
        item = {
            "id": ev.get("id"),
            "calendar_id": cal_id,
            "summary": ev.get("summary"),
            "start": dig(ev, "start", "dateTime", default=dig(ev, "start", "date")),
            "end": dig(ev, "end", "dateTime", default=dig(ev, "end", "date")),
            "location": ev.get("location"),
        }
        if ev.get("visibility") and ev["visibility"] != "default":
            item["visibility"] = ev["visibility"]
        guests = _read_guests(ev)
        if guests:
            item["attendees"] = guests
        out.append(item)
    return cal_id, out, None


@mcp.tool()
async def calendar_events(time_min: str, time_max: str, calendar_id: str = "primary",
                          max_results: int = 25) -> dict:
    """Liste les événements entre deux instants, sur un ou plusieurs agendas.

    time_min / time_max : ISO 8601 (ex. 2026-07-21T00:00:00+02:00).
    calendar_id : « primary » (défaut) = l'agenda principal ; un identifiant rendu
      par `calendar_list` ; plusieurs séparés par des virgules ; ou « * » pour tous
      les agendas du compte.
    max_results : budget TOTAL d'événements rendus, tous agendas confondus.

    Chaque événement porte le `calendar_id` d'où il vient : c'est celui-là qu'il
    faut repasser à `calendar_update`, un identifiant d'événement n'ayant de sens
    que dans son agenda.
    """
    auth = await _authed()
    if isinstance(auth, dict):
        return auth
    _, headers = auth
    limit = max(1, min(int(max_results), 250))
    async with _client() as http:
        ids = _items(calendar_id) or ["primary"]
        note = None
        if "*" in ids:
            cals = await _calendar_ids(http, headers)
            if isinstance(cals, dict):
                return cals
            ids = [c["calendar_id"] for c in cals if c.get("calendar_id")]
            if len(ids) > MAX_CALENDARS:
                note = ("%d agendas sur le compte, les %d premiers seulement ont été "
                        "interrogés — nommer les agendas voulus dans calendar_id pour "
                        "viser." % (len(ids), MAX_CALENDARS))
                ids = ids[:MAX_CALENDARS]
        results = await asyncio.gather(*(
            _fetch_events(http, headers, cid, time_min, time_max, limit) for cid in ids
        ))
    events, errors = [], {}
    for cal_id, items, err in results:
        if err:
            errors[cal_id] = err
        events.extend(items)
    # Merge key: the day first, so an all-day event ("2026-08-01") sorts before the
    # timed ones of the same day whatever their UTC offset.
    events.sort(key=lambda e: ((e.get("start") or "")[:10], e.get("start") or ""))
    out: dict = {}
    if len(events) > limit:
        # A cut must be visible, measured, and recoverable (cf. mail_thread).
        out["note"] = ("%d événements trouvés, %d rendus — rappeler avec "
                       "max_results=<n> pour la suite." % (len(events), limit))
        events = events[:limit]
    out["events"] = events
    if note:
        out["note"] = note if "note" not in out else out["note"] + " " + note
    if errors:
        # Named, never swallowed: one unreadable calendar must not pass for an
        # empty day on the others.
        out["agendas_en_erreur"] = errors
    return out


# Invitations. Google decides who gets an EMAIL from `sendUpdates`, and the middle
# value is the interesting one: an attendee on Google Calendar sees the event
# appear whatever we ask, while an attendee without one sees strictly nothing
# unless a mail goes out. Hence the default: mail exactly those who would
# otherwise be invited to nothing.
#
# ⚠️ This is the FIRST outbound channel of the whole addon - which otherwise
# guarantees "no send" structurally, by having no send tool at all. Here the
# recipient and the text (summary, description) are both caller-chosen, so an
# invitation IS an exfiltration path. Nothing in this module can close it: who may
# be invited is contextual policy, and it belongs to the calling agent's guard
# (channel, human confirmation, allowlist). Said plainly rather than papered over.
SEND_UPDATES = {
    "all": "tous les invités reçoivent un mail",
    "externalOnly": "seuls les invités hors Google Calendar reçoivent un mail",
    "none": "aucun mail — l'événement apparaît seulement chez les invités Google",
}
DEFAULT_SEND_UPDATES = "externalOnly"


def _send_updates(value: str | None) -> str | dict:
    v = (value or DEFAULT_SEND_UPDATES).strip()
    match = {k.lower(): k for k in SEND_UPDATES}.get(v.lower())
    if not match:
        return {"error": "send_updates « %s » inconnu : %s." % (
            value, " / ".join("%s (%s)" % (k, d) for k, d in SEND_UPDATES.items()))}
    return match


@mcp.tool()
async def calendar_create(summary: str, start: str, end: str,
                          description: str | None = None, location: str | None = None,
                          calendar_id: str = "primary", visibility: str | None = None,
                          attendees: str | None = None,
                          send_updates: str | None = None) -> dict:
    """Crée un événement dans un agenda (sur demande explicite de l'utilisateur).

    start / end : ISO 8601 (datetime), ou date seule YYYY-MM-DD pour du journée entière.
    calendar_id : « primary » (défaut), ou un identifiant rendu par `calendar_list`.
    visibility : « private » pour masquer le détail aux autres lecteurs de l'agenda
      (ils voient l'occupation, pas le contenu) ; « public », ou « default » qui
      suit le réglage de l'agenda.
    attendees : adresses mail des invités, séparées par des virgules.
    send_updates : qui reçoit un MAIL d'invitation. « externalOnly » (défaut) =
      seulement les invités hors Google Calendar, qui sans mail ne verraient rien ;
      « all » = tout le monde, en plus de l'invitation posée dans l'agenda ;
      « none » = personne. Un invité sur Google Calendar voit l'événement
      apparaître dans son agenda dans les trois cas.
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
    vis = _visibility(visibility)
    if isinstance(vis, dict):
        return vis
    if vis:
        event["visibility"] = vis
    guests = _guests(attendees)
    if isinstance(guests, dict):
        return guests
    if guests:
        event["attendees"] = guests
    notify = _send_updates(send_updates)
    if isinstance(notify, dict):
        return notify
    async with _client() as http:
        r = await http.post(f"{_cal_path(calendar_id)}/events", json=event,
                            params={"sendUpdates": notify}, headers=headers)
        data = r.json()
    if r.status_code != 200:
        return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
    out = {"id": data.get("id"), "calendar_id": calendar_id,
           "status": "événement créé", "link": data.get("htmlLink")}
    if guests:
        out["invites"] = "%d invité(s) — %s." % (len(guests), SEND_UPDATES[notify])
    return out


@mcp.tool()
async def calendar_update(event_id: str, summary: str | None = None, start: str | None = None,
                          end: str | None = None, description: str | None = None,
                          location: str | None = None, calendar_id: str = "primary",
                          visibility: str | None = None, attendees: str | None = None,
                          send_updates: str | None = None) -> dict:
    """Modifie un événement existant (déplacement, renommage…) — confirmation utilisateur requise en amont.

    calendar_id : l'agenda où vit l'événement — celui que `calendar_events` a rendu
      à côté de lui. Un identifiant d'événement n'existe que dans son agenda ;
      viser le mauvais agenda rend « not found », pas une modification silencieuse.
    attendees : REMPLACE la liste d'invités, ne s'y ajoute pas — repasser la liste
      complète. Un invité retiré de la liste est désinvité.
    send_updates : qui reçoit un mail (cf. `calendar_create`). Sur une modification
      il prévient AUSSI les invités déjà présents que l'événement a bougé.

    Ne déplace pas un événement d'un agenda à l'autre : aucun outil ne fait ça.
    """
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
    vis = _visibility(visibility)
    if isinstance(vis, dict):
        return vis
    if vis:
        patch["visibility"] = vis
    guests = _guests(attendees)
    if isinstance(guests, dict):
        return guests
    if attendees is not None:
        patch["attendees"] = guests
    notify = _send_updates(send_updates)
    if isinstance(notify, dict):
        return notify
    if not patch:
        return {"error": "rien à modifier : aucun champ fourni."}
    async with _client() as http:
        r = await http.patch(f"{_cal_path(calendar_id)}/events/{quote(event_id, safe='')}",
                             json=patch, params={"sendUpdates": notify}, headers=headers)
        data = r.json()
    if r.status_code != 200:
        return {"error": dig(data, "error", "message", default=f"HTTP {r.status_code}")}
    out = {"id": data.get("id"), "calendar_id": calendar_id,
           "status": "événement modifié", "link": data.get("htmlLink")}
    if attendees is not None:
        out["invites"] = "liste d'invités remplacée (%d) — %s." % (len(guests), SEND_UPDATES[notify])
    return out


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
    return enrol_page("Google", glyph, title, message, status)


# Extrait dans `_common` le 2026-07-31 : la même fonction avait été re-tapée SANS
# la récupération latin-1 dans `github`, produisant un enrôlement rangé sous une
# clé qu'aucun appel ne retrouve. Une seule implémentation, un seul endroit où se
# tromper — et le prochain addon hérite du correctif, pas du bug.
_remote_user = remote_user


async def enroll(request):
    sub = _remote_user(request)
    if not sub:
        return _page("🚪", "Accès refusé", "Cette page passe par le SSO de la maison — pas par la porte de service.", 403)
    client = _oauth_client()
    if isinstance(client, str):
        return _page("🧩", "Configuration absente", client, 500)
    external = os.environ.get("ROSETTA_EXTERNAL_URL", "").rstrip("/")
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
    external = os.environ.get("ROSETTA_EXTERNAL_URL", "").rstrip("/")
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
