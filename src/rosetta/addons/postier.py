"""`postier` addon - the ONE sending capability of the hub, machine-callable.

Everything else in the mail family is read-or-draft by design; this module is
the single, deliberately narrow exception, so that Nestor (a machine identity,
which /mail refuses) can actually mail the household. The blast radius is
bounded by construction, not by trust:

  - the sender is FROZEN to `POSTIER_FROM` (the assistant's own mailbox) -
    no tool argument can impersonate a human;
  - every recipient must match `POSTIER_ALLOWED` (default: the family domain) -
    a hijacked agent writes an awkward email to the family, not spam to the
    world;
  - a sliding-window rate limit (`POSTIER_MAX_PER_HOUR`) keeps even a looping
    agent polite;
  - every send is logged and copied to the Sent folder - auditable after the
    fact from any mail client.

SMTP submission does NOT file a copy anywhere: saving to Sent is the client's
job, so the tool appends one over IMAP after sending - best effort, reported
in the answer rather than fatal (the mail is already gone).

Tool description in French - runtime UX for the household agents.
"""

from __future__ import annotations

import imaplib
import logging
import os
import smtplib
import time
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid
from fnmatch import fnmatch

from ._common import new_server

logger = logging.getLogger("rosetta.postier")

required_env = ["POSTIER_FROM", "POSTIER_PASSWORD"]

mcp = new_server("postier")

# Test seams.
_smtp_factory = None
_imap_factory = None
_sent_at: list[float] = []  # sliding window; one replica by design (like withings)


def _allowed_patterns() -> list[str]:
    raw = os.environ.get("POSTIER_ALLOWED", "*@berard.me")
    return [p.strip().casefold() for p in raw.split(",") if p.strip()]


def _refused(recipients: list[str]) -> list[str]:
    patterns = _allowed_patterns()
    return [r for r in recipients
            if not any(fnmatch(r.casefold(), p) for p in patterns)]


def _rate_limited(now: float | None = None) -> bool:
    now = now or time.time()
    horizon = now - 3600
    _sent_at[:] = [t for t in _sent_at if t > horizon]
    return len(_sent_at) >= int(os.environ.get("POSTIER_MAX_PER_HOUR", "10"))


def _smtp():
    if _smtp_factory is not None:
        return _smtp_factory()
    host = os.environ.get("POSTIER_SMTP_HOST", "smtp.mail.ovh.net")
    client = smtplib.SMTP_SSL(host, 465, timeout=30)
    client.login(os.environ["POSTIER_FROM"], os.environ["POSTIER_PASSWORD"])
    return client


def _imap():
    if _imap_factory is not None:
        return _imap_factory()
    host = os.environ.get("POSTIER_IMAP_HOST", "imap.mail.ovh.net")
    client = imaplib.IMAP4_SSL(host, 993)
    client.login(os.environ["POSTIER_FROM"], os.environ["POSTIER_PASSWORD"])
    return client


@mcp.tool()
def envoyer_mail(a: str, sujet: str, corps: str, cc: str = "") -> dict | str:
    """Envoie un mail DEPUIS la boîte de l'assistant (expéditeur verrouillé).
    Destinataires limités à la famille (et aux adresses explicitement
    autorisées) ; quota horaire ; copie dans Sent. Pour écrire au nom d'un
    humain, utiliser mail_brouillon de /mail : ici on n'usurpe personne."""
    sender = os.environ["POSTIER_FROM"]
    recipients = [addr for _, addr in getaddresses([a] + ([cc] if cc else [])) if addr]
    if not recipients:
        return "aucun destinataire exploitable dans `a`."
    if refused := _refused(recipients):
        return (f"destinataire(s) hors liste autorisée : {', '.join(refused)}. "
                "L'allowlist se gère côté serveur (POSTIER_ALLOWED).")
    if _rate_limited():
        return "quota d'envoi horaire atteint — le postier reprend son souffle."

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = a
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = sujet
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.partition("@")[2])
    msg.set_content(corps or "")

    client = _smtp()
    try:
        client.send_message(msg)
    finally:
        try:
            client.quit()
        except Exception:
            pass
    _sent_at.append(time.time())
    logger.info("postier: mail envoyé à %s (sujet: %s)", ", ".join(recipients), sujet)

    copie = "copié dans Sent"
    try:
        imap = _imap()
        try:
            status, _ = imap.append(
                os.environ.get("POSTIER_SENT_FOLDER", "Sent"), r"(\Seen)",
                imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            if status != "OK":
                copie = f"copie Sent en échec ({status})"
        finally:
            imap.logout()
    except Exception as exc:
        copie = f"copie Sent en échec ({type(exc).__name__})"

    return {"envoyé": True, "de": sender, "a": recipients, "sujet": sujet,
            "copie": copie}
