"""`mail` addon - the family's own mailboxes (OVH Zimbra), user-data class.

The sovereign twin of `google`: same philosophy (read + **drafts only** -
deliberately no send, no delete: the human reviews the draft in their client
and presses the button), but over plain IMAP against the household's Zimbra
platform, plus the one thing Gmail never had - **disposable aliases** through
the OVH v2 API (create one per merchant, burn it at the first spam).

Identity: `identity = "user"` - the hub refuses machine tokens on /mail, so
every call carries a human subject. The mailbox is DERIVED from that identity
(`mail_local` claim, an Authelia CEL attribute = the email local part; the
accent-folded `preferred_username` is the transition fallback), and the
password is NOT provisioned to this pod at all: the addon EXCHANGES the
caller's own bearer against a vault token (OpenBao JWT auth federated on
Authelia) whose templated policy only opens `creds/<mail_local>`. The
capability follows the SSO session and the VAULT arbitrates - a bug here
cannot read another member's mailbox, because this process never could.
Alias tools are self-service *bounded by construction*: they only ever list,
create or delete aliases pointing at the CALLER's own account.

Two IMAP details shape the module:

  1. Everything speaks UIDs (`uid('search')`/`uid('fetch')`), never sequence
     numbers: a sequence number silently designates ANOTHER message after any
     expunge; an agent quoting yesterday's numbers would read the wrong mail.
  2. A draft reply must chain `In-Reply-To`/`References` from the original and
     honour `Reply-To` over `From`, or the recipient's client starts a new
     thread and the reply lands out of context.

Tool descriptions are in French - runtime UX for the household agents.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import imaplib
import json
import os
import re
import time
import unicodedata
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parsedate_to_datetime

import httpx

from ..auth import current_claims, current_token
from ._common import TIMEOUT, new_server

identity = "user"

required_env = ["MAIL_IMAP_HOST", "MAIL_DOMAIN"]

mcp = new_server("mail")

OVH_API = "https://eu.api.ovh.com"
BODY_MAX = 20_000  # characters of body text returned by mail_lire
PW_CACHE_TTL = 600  # seconds a fetched mailbox password stays in memory

# Test seams: tests swap the IMAP factory and the httpx transports, no network.
_imap_factory = None
_transport = None
_vault_transport = None

_ovh_cache: dict = {}  # platform/account ids: stable for the platform's lifetime
_pw_cache: dict = {}   # local -> (password, expiry): spares the vault a login per call


# ---- identity -> mailbox ----------------------------------------------------

def _norm(name: str) -> str:
    """`Sébastien` -> `sebastien`: the same folding creds-sync applies, so the
    identity maps onto the mailboxes it provisioned."""
    flat = "".join(c for c in unicodedata.normalize("NFD", name)
                   if unicodedata.category(c) != "Mn").casefold()
    return re.sub(r"[^a-z0-9._-]", "", flat)


def _caller() -> tuple[str, str] | str:
    """(email, password) of the caller's mailbox, or a French error string.

    The password comes from the vault, unlocked BY THE CALLER'S OWN TOKEN:
    OpenBao's JWT auth validates it against Authelia and issues a token whose
    templated policy only reads `creds/<mail_local>`. This process holds no
    mailbox password of its own - it can only open what the caller could."""
    claims = current_claims.get() or {}
    local = str(claims.get("mail_local") or "").strip() or _norm(
        unicodedata.normalize("NFC", str(claims.get("preferred_username") or "")))
    if not local:
        return "identité introuvable dans le token — appel hors contexte utilisateur ?"
    email_addr = f"{local}@{os.environ['MAIL_DOMAIN']}"

    hit = _pw_cache.get(local)
    if hit and hit[1] > time.time():
        return email_addr, hit[0]

    token = current_token.get()
    if not token:
        return "token brut indisponible — hub démarré sans middleware d'auth ?"
    vault = os.environ.get("MAIL_VAULT_ADDR", "https://vault.berard.me").rstrip("/")
    mount = os.environ.get("MAIL_VAULT_MOUNT", "jwt-authelia")
    role = os.environ.get("MAIL_VAULT_ROLE", "rosetta-mail")
    with httpx.Client(transport=_vault_transport, timeout=TIMEOUT) as client:
        r = client.post(f"{vault}/v1/auth/{mount}/login",
                        json={"role": role, "jwt": token})
        if r.status_code != 200:
            return (f"le coffre a refusé ton identité (HTTP {r.status_code}) — "
                    f"mount {mount}/rôle {role} en place et token encore valide ?")
        vtok = r.json()["auth"]["client_token"]
        r = client.get(f"{vault}/v1/secret/data/creds/{local}",
                       headers={"X-Vault-Token": vtok})
        if r.status_code != 200:
            return (f"le coffre n'ouvre pas creds/{local} pour cette identité "
                    f"(HTTP {r.status_code}).")
        password = (r.json().get("data", {}).get("data", {}) or {}).get("password", "")
    if not password:
        return f"creds/{local} existe mais n'a pas de champ password."
    _pw_cache[local] = (password, time.time() + PW_CACHE_TTL)
    return email_addr, password


def _imap(email_addr: str, password: str):
    if _imap_factory is not None:
        return _imap_factory(email_addr, password)
    client = imaplib.IMAP4_SSL(os.environ["MAIL_IMAP_HOST"], 993)
    client.login(email_addr, password)
    return client


# ---- message parsing --------------------------------------------------------

def _parse(raw: bytes) -> email.message.Message:
    return email.message_from_bytes(raw, policy=email.policy.default)


def _body_text(msg: email.message.Message) -> str:
    """text/plain wins; a lone text/html is crudely flattened rather than lost."""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
    if len(text) > BODY_MAX:
        text = text[:BODY_MAX] + f"\n[… tronqué à {BODY_MAX} caractères]"
    return text.strip()


def _summary(uid: str, msg: email.message.Message, flags: str) -> dict:
    date = msg.get("Date", "")
    try:
        date = parsedate_to_datetime(date).isoformat()
    except Exception:
        pass
    return {
        "uid": uid,
        "de": msg.get("From", ""),
        "sujet": msg.get("Subject", ""),
        "date": date,
        "non_lu": "\\Seen" not in flags,
    }


_UID_FLAGS = re.compile(rb"UID (\d+)")
_FLAGS = re.compile(rb"FLAGS \(([^)]*)\)")


# ---- tools : lecture --------------------------------------------------------

@mcp.tool()
def mail_dossiers() -> list[str] | str:
    """Liste les dossiers de la boîte mail de l'appelant (INBOX, Sent, Drafts…)."""
    creds = _caller()
    if isinstance(creds, str):
        return creds
    client = _imap(*creds)
    try:
        status, rows = client.list()
        if status != "OK":
            return f"IMAP LIST a répondu {status}"
        out = []
        for row in rows or []:
            m = re.search(rb'(?:"([^"]+)"|(\S+))$', row or b"")
            if m:
                out.append((m.group(1) or m.group(2)).decode("utf-7", errors="replace"))
        return out
    finally:
        client.logout()


@mcp.tool()
def mail_recherche(dossier: str = "INBOX", de: str = "", sujet: str = "",
                   depuis_jours: int = 0, non_lus: bool = False,
                   limite: int = 20) -> list[dict] | str:
    """Cherche dans la boîte de l'appelant. Filtres cumulables : expéditeur
    (`de`), texte du sujet (`sujet`), fenêtre en jours (`depuis_jours`),
    non-lus seulement (`non_lus`). Rend les plus récents d'abord, avec l'`uid`
    à passer à mail_lire / mail_brouillon."""
    creds = _caller()
    if isinstance(creds, str):
        return creds
    criteria: list[str] = []
    if de:
        criteria += ["FROM", f'"{de}"']
    if sujet:
        criteria += ["SUBJECT", f'"{sujet}"']
    if depuis_jours > 0:
        since = time.strftime("%d-%b-%Y", time.localtime(time.time() - depuis_jours * 86400))
        criteria += ["SINCE", since]
    if non_lus:
        criteria.append("UNSEEN")
    client = _imap(*creds)
    try:
        status, _ = client.select(dossier, readonly=True)
        if status != "OK":
            return f"dossier « {dossier} » introuvable"
        status, rows = client.uid("search", None, *(criteria or ["ALL"]))
        if status != "OK":
            return f"IMAP SEARCH a répondu {status}"
        uids = (rows[0] or b"").split()
        out = []
        for uid in reversed(uids[-max(1, min(limite, 100)):]):
            status, data = client.uid(
                "fetch", uid,
                "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if status != "OK" or not data or data[0] is None:
                continue
            flags = (_FLAGS.search(data[0][0] or b"") or [b""])
            flags = flags.group(1).decode() if hasattr(flags, "group") else ""
            out.append(_summary(uid.decode(), _parse(data[0][1]), flags))
        return out
    finally:
        client.logout()


@mcp.tool()
def mail_lire(uid: str, dossier: str = "INBOX") -> dict | str:
    """Lit un message complet (en-têtes + corps texte) par son `uid`.
    Ne marque PAS le message comme lu."""
    creds = _caller()
    if isinstance(creds, str):
        return creds
    client = _imap(*creds)
    try:
        status, _ = client.select(dossier, readonly=True)
        if status != "OK":
            return f"dossier « {dossier} » introuvable"
        status, data = client.uid("fetch", uid.encode(), "(FLAGS BODY.PEEK[])")
        if status != "OK" or not data or data[0] is None:
            return f"message {uid} introuvable dans {dossier}"
        flags_m = _FLAGS.search(data[0][0] or b"")
        msg = _parse(data[0][1])
        return {
            **_summary(uid, msg, flags_m.group(1).decode() if flags_m else ""),
            "a": msg.get("To", ""),
            "cc": msg.get("Cc", ""),
            "message_id": msg.get("Message-ID", ""),
            "corps": _body_text(msg),
            "pieces_jointes": [
                part.get_filename() or "(sans nom)"
                for part in msg.iter_attachments()
            ],
        }
    finally:
        client.logout()


# ---- tools : brouillons -----------------------------------------------------

@mcp.tool()
def mail_brouillon(a: str = "", sujet: str = "", corps: str = "",
                   cc: str = "", en_reponse_a: str = "",
                   dossier_source: str = "INBOX") -> dict | str:
    """Dépose un BROUILLON dans la boîte de l'appelant (jamais d'envoi : le
    brouillon se relit et s'envoie depuis le client mail). Pour répondre à un
    message, passer son `uid` dans `en_reponse_a` : destinataire, sujet et fil
    de discussion sont repris de l'original (`a` explicite prime)."""
    creds = _caller()
    if isinstance(creds, str):
        return creds
    sender, password = creds
    client = _imap(sender, password)
    try:
        draft = EmailMessage()
        if en_reponse_a:
            status, _ = client.select(dossier_source, readonly=True)
            if status != "OK":
                return f"dossier « {dossier_source} » introuvable"
            status, data = client.uid("fetch", en_reponse_a.encode(), "(BODY.PEEK[])")
            if status != "OK" or not data or data[0] is None:
                return f"message {en_reponse_a} introuvable dans {dossier_source}"
            orig = _parse(data[0][1])
            # Reply-To beats From; an explicit `a` beats both.
            a = a or orig.get("Reply-To") or orig.get("From") or ""
            base = (orig.get("Subject") or "").strip()
            sujet = sujet or (base if base.lower().startswith("re:") else f"Re: {base}")
            if orig.get("Message-ID"):
                draft["In-Reply-To"] = orig["Message-ID"]
                refs = (orig.get("References", "") + " " + orig["Message-ID"]).strip()
                draft["References"] = refs
        if not a:
            return "aucun destinataire : passer `a` ou `en_reponse_a`."
        draft["From"] = sender
        draft["To"] = a
        if cc:
            draft["Cc"] = cc
        draft["Subject"] = sujet
        draft["Date"] = formatdate(localtime=True)
        draft["Message-ID"] = make_msgid(domain=os.environ["MAIL_DOMAIN"])
        draft.set_content(corps or "")
        folder = os.environ.get("MAIL_DRAFTS_FOLDER", "Drafts")
        status, _ = client.append(
            folder, r"(\Draft)", imaplib.Time2Internaldate(time.time()),
            draft.as_bytes())
        if status != "OK":
            return f"IMAP APPEND vers {folder} a répondu {status}"
        return {"brouillon": "créé", "dossier": folder, "de": sender,
                "a": a, "sujet": sujet}
    finally:
        client.logout()


# ---- tools : alias jetables (API OVH) ----------------------------------------

def _ovh(method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
    ak = os.environ.get("OVH_APPLICATION_KEY")
    a_s = os.environ.get("OVH_APPLICATION_SECRET")
    ck = os.environ.get("OVH_CONSUMER_KEY")
    if not (ak and a_s and ck):
        return 0, "clés API OVH non provisionnées (env OVH_*) — alias indisponibles."
    body = json.dumps(payload) if payload is not None else ""
    with httpx.Client(transport=_transport, timeout=TIMEOUT) as client:
        ts = client.get(f"{OVH_API}/1.0/auth/time").text.strip()
        sig = "$1$" + hashlib.sha1(
            "+".join([a_s, ck, method, OVH_API + path, body, ts]).encode()).hexdigest()
        r = client.request(
            method, OVH_API + path, content=body or None,
            headers={"X-Ovh-Application": ak, "X-Ovh-Consumer": ck,
                     "X-Ovh-Timestamp": ts, "X-Ovh-Signature": sig,
                     **({"Content-Type": "application/json"} if body else {})})
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text


def _my_account_id(caller_email: str) -> tuple[str, str] | str:
    """(platform_id, account_id) of the caller's mailbox, cached."""
    if "platform" not in _ovh_cache:
        code, platforms = _ovh("GET", "/v2/zimbra/platform")
        if code != 200 or not platforms:
            return f"plateforme Zimbra injoignable (HTTP {code})"
        _ovh_cache["platform"] = platforms[0]["id"]
    pid = _ovh_cache["platform"]
    if caller_email not in _ovh_cache:
        code, accounts = _ovh("GET", f"/v2/zimbra/platform/{pid}/account")
        if code != 200:
            return f"liste des comptes Zimbra KO (HTTP {code})"
        for acc in accounts:
            _ovh_cache[acc["currentState"]["email"]] = acc["id"]
    if caller_email not in _ovh_cache:
        return f"aucun compte Zimbra pour {caller_email}"
    return pid, _ovh_cache[caller_email]


def _my_aliases(pid: str, account_id: str) -> list[dict] | str:
    code, rows = _ovh("GET", f"/v2/zimbra/platform/{pid}/alias")
    if code != 200:
        return f"liste des alias KO (HTTP {code})"
    return [
        {"id": row["id"], "alias": row["currentState"]["alias"]["name"],
         "etat": row["resourceStatus"]}
        for row in rows
        if row["currentState"].get("target", {}).get("id") == account_id
    ]


@mcp.tool()
def mail_alias_liste() -> list[dict] | str:
    """Liste les alias jetables qui pointent vers la boîte de l'appelant."""
    creds = _caller()
    if isinstance(creds, str):
        return creds
    ids = _my_account_id(creds[0])
    if isinstance(ids, str):
        return ids
    return _my_aliases(*ids)


@mcp.tool()
def mail_alias_creer(nom_local: str) -> dict | str:
    """Crée un alias jetable `<nom_local>@<domaine>` vers la boîte de
    l'appelant (anti-spam : un alias par marchand, à supprimer au premier
    abus via mail_alias_supprimer)."""
    creds = _caller()
    if isinstance(creds, str):
        return creds
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,63}", nom_local):
        return "nom d'alias invalide : minuscules, chiffres, . _ - (2 à 64 caractères)."
    ids = _my_account_id(creds[0])
    if isinstance(ids, str):
        return ids
    pid, account_id = ids
    alias = f"{nom_local}@{os.environ['MAIL_DOMAIN']}"
    code, resp = _ovh("POST", f"/v2/zimbra/platform/{pid}/alias",
                      {"targetSpec": {"alias": alias, "targetId": account_id}})
    if code not in (200, 202):
        detail = resp.get("message", resp) if isinstance(resp, dict) else resp
        return f"création refusée (HTTP {code}) : {detail}"
    return {"alias": alias, "vers": creds[0], "etat": "création en cours (~10 s)"}


@mcp.tool()
def mail_alias_supprimer(nom_local: str) -> dict | str:
    """Supprime un alias jetable de l'appelant (le marchand a vendu l'adresse ?
    l'alias meurt, la boîte survit). Ne peut supprimer QUE ses propres alias."""
    creds = _caller()
    if isinstance(creds, str):
        return creds
    ids = _my_account_id(creds[0])
    if isinstance(ids, str):
        return ids
    pid, account_id = ids
    aliases = _my_aliases(pid, account_id)
    if isinstance(aliases, str):
        return aliases
    wanted = f"{nom_local}@{os.environ['MAIL_DOMAIN']}"
    target = next((a for a in aliases if a["alias"] == wanted), None)
    if target is None:
        return f"aucun alias « {wanted} » ne pointe vers ta boîte."
    code, _ = _ovh("DELETE", f"/v2/zimbra/platform/{pid}/alias/{target['id']}")
    if code not in (200, 202, 204):
        return f"suppression refusée (HTTP {code})"
    return {"alias": wanted, "etat": "supprimé"}
