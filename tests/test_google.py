"""google addon: credential store, enrolment flow, and tool behaviour against
a mocked Google API (httpx.MockTransport - no network)."""

import asyncio
import base64
import json

import httpx
import pytest

from rosetta.addons import google
from rosetta.auth import current_claims


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ROSETTA_GOOGLE_DATA", str(tmp_path))
    (tmp_path / "client_secret.json").write_text(
        json.dumps({"web": {"client_id": "cid", "client_secret": "csec"}})
    )
    return tmp_path


@pytest.fixture
def enrolled(data_dir):
    users = data_dir / "users"
    users.mkdir()
    (users / "sebastien.json").write_text(json.dumps({
        "sub": "sebastien", "refresh_token": "rt-123", "scopes": [], "enrolled_at": 0,
    }))
    google._token_cache.clear()
    current_claims.set({"sub": "sebastien"})
    return data_dir


def mock(handler):
    return httpx.MockTransport(handler)


def run(coro):
    return asyncio.run(coro)


def test_unenrolled_user_gets_actionable_error(data_dir):
    current_claims.set({"sub": "quelqu-un"})
    google._token_cache.clear()
    out = run(google.mail_search("from:test"))
    assert "enroll" in out["error"]


def test_mail_search_and_token_refresh(enrolled, monkeypatch):
    calls = []

    def handler(request):
        calls.append(str(request.url.path))
        if request.url.host == "oauth2.googleapis.com":
            assert b"refresh_token=rt-123" in request.read()
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        return httpx.Response(200, json={
            "id": "m1", "threadId": "t1", "snippet": "extrait",
            "payload": {"headers": [
                {"name": "From", "value": "a@b.c"},
                {"name": "Subject", "value": "Résa"},
                {"name": "Date", "value": "Mon, 20 Jul 2026"},
            ]},
        })

    monkeypatch.setattr(google, "_transport", mock(handler))
    out = run(google.mail_search("from:a@b.c"))
    assert out["messages"][0]["subject"] == "Résa"
    assert out["messages"][0]["thread_id"] == "t1"
    # token endpoint hit exactly once, then cached
    run(google.mail_search("again"))
    assert sum(1 for c in calls if c == "/token") == 1


def test_mail_draft_builds_reply_mime(enrolled, monkeypatch):
    captured = {}

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if "/threads/" in request.url.path:
            return httpx.Response(200, json={"messages": [
                {"payload": {"headers": [{"name": "Message-ID", "value": "<orig@x>"}]}},
            ]})
        captured.update(json.loads(request.read()))
        return httpx.Response(200, json={"id": "d1", "message": {
            "id": "19faa841267fcac6", "threadId": "19faa000aaaa1111"}})

    monkeypatch.setattr(google, "_transport", mock(handler))
    out = run(google.mail_draft("Bien reçu.", to="x@y.z", subject="Re: Résa", thread_id="t1"))
    assert out["draft_id"] == "d1"
    # Account INDEX (the address form 404s), and the THREAD id (the message id is
    # reminted on every amendment). Verified against the real Gmail UI.
    assert out["link"] == "https://mail.google.com/mail/u/0/#drafts/19faa000aaaa1111"
    assert captured["message"]["threadId"] == "t1"
    raw = base64.urlsafe_b64decode(captured["message"]["raw"]).decode()
    assert "To: x@y.z" in raw and "In-Reply-To: <orig@x>" in raw
    assert "Re: =?utf-8?q?R=C3=A9sa?=" in raw or "Re: Résa" in raw


def test_draft_link_account_is_overridable(enrolled, monkeypatch):
    """Index 0 is the browser's first account; a different mailbox needs the override."""
    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        return httpx.Response(200, json={"id": "d1", "message": {"threadId": "abc123"}})

    monkeypatch.setattr(google, "_transport", mock(handler))
    monkeypatch.setenv("ROSETTA_GMAIL_ACCOUNT", "2")
    out = run(google.mail_draft("Corps.", to="x@y.z", subject="Objet"))
    assert out["link"] == "https://mail.google.com/mail/u/2/#drafts/abc123"


def test_draft_keeps_thread_id_even_if_metadata_fetch_fails(enrolled, monkeypatch):
    """The thread attachment must never depend on the best-effort header fetch."""
    captured = {}

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if "/threads/" in request.url.path:
            return httpx.Response(500, json={})
        captured.update(json.loads(request.read()))
        return httpx.Response(200, json={"id": "d2", "message": {"id": "m2"}})

    monkeypatch.setattr(google, "_transport", mock(handler))
    out = run(google.mail_draft("Corps.", to="x@y.z", subject="Re: Résa", thread_id="t1"))
    assert out["draft_id"] == "d2"
    assert captured["message"]["threadId"] == "t1"
    raw = base64.urlsafe_b64decode(captured["message"]["raw"]).decode()
    assert "In-Reply-To" not in raw  # headers skipped, attachment preserved


def _parent_message(headers, thread_id="t9"):
    """A messages.get format=metadata resource for the message being replied to."""
    return {"id": "m9", "threadId": thread_id,
            "payload": {"headers": [{"name": n, "value": v} for n, v in headers]}}


def test_reply_prefers_reply_to_over_from(enrolled, monkeypatch):
    """The whole point: platforms send from a no-reply address and put the real one
    in Reply-To. Answering From produces a draft that looks right and goes nowhere."""
    captured = {}

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if "/messages/" in request.url.path:
            return httpx.Response(200, json=_parent_message([
                ("From", "no-reply@plateforme.fr"),
                ("Reply-To", "contact@lekastor.fr"),
                ("Subject", "Votre demande de devis"),
                ("Message-ID", "<parent@x>"),
                ("References", "<ancien@x>"),
            ]))
        captured.update(json.loads(request.read()))
        return httpx.Response(200, json={"id": "d9", "message": {"threadId": "t9"}})

    monkeypatch.setattr(google, "_transport", mock(handler))
    out = run(google.mail_draft("Merci, c'est noté.", reply_to_message_id="m9"))
    assert out["to"] == "contact@lekastor.fr"        # NOT no-reply@plateforme.fr
    assert out["subject"] == "Re: Votre demande de devis"
    assert captured["message"]["threadId"] == "t9"   # thread derived, not asked for
    raw = base64.urlsafe_b64decode(captured["message"]["raw"]).decode()
    assert "In-Reply-To: <parent@x>" in raw
    # References carries the parent's chain PLUS the parent itself.
    assert "References: <ancien@x> <parent@x>" in raw


def test_reply_derivation_is_overridable_and_avoids_re_re(enrolled, monkeypatch):
    """An explicit `to` wins over the derived one, and an already-Re: subject is
    not prefixed twice."""
    captured = {}

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if "/messages/" in request.url.path:
            return httpx.Response(200, json=_parent_message([
                ("From", "a@b.c"), ("Subject", "Re: déjà une réponse"),
                ("Message-ID", "<p@x>"),
            ]))
        captured.update(json.loads(request.read()))
        return httpx.Response(200, json={"id": "d9", "message": {"threadId": "t9"}})

    monkeypatch.setattr(google, "_transport", mock(handler))
    out = run(google.mail_draft("Corps.", to="autre@z.z", reply_to_message_id="m9"))
    assert out["to"] == "autre@z.z"
    assert out["subject"] == "Re: déjà une réponse"   # pas « Re: Re: … »


def test_draft_without_recipient_is_refused(enrolled, monkeypatch):
    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        raise AssertionError("no draft should be posted without a recipient")

    monkeypatch.setattr(google, "_transport", mock(handler))
    assert "destinataire" in run(google.mail_draft("Corps sans personne à qui parler."))["error"]


def _draft_resource(to="x@y.z", subject="Re: Résa", body="Corps initial.",
                    thread_id="t1", extra_headers=()):
    """A drafts.get format=full resource, as Gmail returns it."""
    headers = [{"name": "To", "value": to}, {"name": "Subject", "value": subject},
               {"name": "Date", "value": "Mon, 27 Jul 2026"}]
    headers += [{"name": n, "value": v} for n, v in extra_headers]
    return {"id": "d1", "message": {
        "id": "19faa841267fcac6", "threadId": thread_id,
        "payload": {"mimeType": "text/plain", "headers": headers,
                    "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}},
    }}


def test_mail_drafts_lists_and_reads(enrolled, monkeypatch):
    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if request.url.path.endswith("/drafts"):
            return httpx.Response(200, json={"drafts": [{"id": "d1"}]})
        return httpx.Response(200, json=_draft_resource())

    monkeypatch.setattr(google, "_transport", mock(handler))
    listed = run(google.mail_drafts())
    assert listed["drafts"] == [{
        "draft_id": "d1", "thread_id": "t1", "to": "x@y.z", "subject": "Re: Résa",
        "date": "Mon, 27 Jul 2026",
        "link": "https://mail.google.com/mail/u/0/#drafts/t1",
    }]
    one = run(google.mail_drafts(draft_id="d1"))
    assert one["body"] == "Corps initial." and one["subject"] == "Re: Résa"


def test_mail_draft_update_merges_and_keeps_thread(enrolled, monkeypatch):
    """PUT replaces the whole draft: unspecified fields and the thread attachment
    must be carried over from the current draft, not dropped."""
    captured = {}

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if request.method == "PUT":
            captured.update(json.loads(request.read()))
            # Gmail hands back a transient message id here: it only lands on the
            # #drafts folder, so the link must NOT be built from this response.
            return httpx.Response(200, json={"id": "d1", "message": {"id": "ffff0000transient"}})
        return httpx.Response(200, json=_draft_resource(
            extra_headers=[("In-Reply-To", "<orig@x>"), ("References", "<a@x> <orig@x>")]))

    monkeypatch.setattr(google, "_transport", mock(handler))
    out = run(google.mail_draft_update("d1", body="Corps corrigé."))
    assert out["draft_id"] == "d1"
    # Same stable link as the deposit returned: built on the thread, not on the
    # message id, so a link handed out before the amendment still works.
    assert out["link"] == "https://mail.google.com/mail/u/0/#drafts/t1"
    assert captured["message"]["threadId"] == "t1"  # never silently detached
    raw = base64.urlsafe_b64decode(captured["message"]["raw"]).decode()
    assert "Corps corrigé." in raw and "Corps initial." not in raw
    assert "To: x@y.z" in raw                       # untouched field preserved
    assert "In-Reply-To: <orig@x>" in raw and "References: <a@x> <orig@x>" in raw


def test_mail_draft_update_rejects_empty_patch(enrolled, monkeypatch):
    def handler(request):  # must never be reached
        raise AssertionError("no HTTP call expected for an empty patch")

    monkeypatch.setattr(google, "_transport", mock(handler))
    assert "error" in run(google.mail_draft_update("d1"))


def test_mail_draft_update_reports_unknown_draft(enrolled, monkeypatch):
    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        return httpx.Response(404, json={"error": {"message": "Requested entity was not found."}})

    monkeypatch.setattr(google, "_transport", mock(handler))
    out = run(google.mail_draft_update("nope", body="x"))
    assert "not found" in out["error"]


def test_store_keyed_on_preferred_username(enrolled, monkeypatch):
    """Authelia access tokens may carry an opaque sub: the username claim wins."""
    current_claims.set({"sub": "opaque-uuid-1234", "preferred_username": "sebastien"})

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        return httpx.Response(200, json={"messages": []})

    monkeypatch.setattr(google, "_transport", mock(handler))
    out = run(google.mail_search("x"))
    assert "error" not in out  # resolved the sebastien.json credential


def test_no_send_tool_exists():
    tool_names = {t.name for t in run(google.mcp.list_tools())}
    assert tool_names == {
        "mail_search", "mail_thread", "mail_attachment",
        "mail_draft", "mail_drafts", "mail_draft_update",
        "calendar_events", "calendar_create", "calendar_update",
    }
    # The point of pinning the set: no send, no delete, no label tool ever slips in.
    assert not any(("send" in n) or ("delete" in n) or ("label" in n) for n in tool_names)


def test_state_sign_and_verify(data_dir):
    state = google._sign_state("sebastien")
    assert google._verify_state(state) == "sebastien"
    # Forgery: altering the signed payload without re-signing must fail.
    expiry, _sub, sig = base64.urlsafe_b64decode(state.encode()).decode().split(".", 2)
    forged = base64.urlsafe_b64encode(f"{expiry}.attacker.{sig}".encode()).decode()
    assert google._verify_state(forged) is None
    assert google._verify_state("garbage") is None


def test_enrolment_flow_end_to_end(data_dir, monkeypatch):
    """Browser flow: forwardAuth header -> Google consent redirect -> callback
    stores the per-user refresh token. JWT-exempt but SSO-guarded paths."""
    from urllib.parse import parse_qs, urlparse

    from starlette.testclient import TestClient

    from rosetta.main import create_app

    monkeypatch.setenv("ROSETTA_AUTH", "oidc")  # auth ON: enroll must be open
    app = create_app()
    with TestClient(app) as client:
        # Without the forwardAuth header: refused.
        assert client.get("/google/enroll", follow_redirects=False).status_code == 403
        r = client.get("/google/enroll", headers={"Remote-User": "sebastien"},
                       follow_redirects=False)
        assert r.status_code == 302
        target = urlparse(r.headers["location"])
        assert target.hostname == "accounts.google.com"
        query = parse_qs(target.query)
        assert query["access_type"] == ["offline"]
        state = query["state"][0]

        def handler(request):
            assert b"grant_type=authorization_code" in request.read()
            return httpx.Response(200, json={
                "access_token": "at", "refresh_token": "rt-new", "scope": "a b",
            })

        monkeypatch.setattr(google, "_transport", mock(handler))
        r = client.get(f"/google/callback?code=abc&state={state}")
        assert r.status_code == 200 and "sebastien" in r.text
    stored = json.loads((data_dir / "users" / "sebastien.json").read_text())
    assert stored["refresh_token"] == "rt-new"


def test_enrolment_recovers_utf8_mangled_header(data_dir, monkeypatch):
    """HTTP headers travel as latin-1 while Authelia emits UTF-8: the accents
    of « Sébastien » must survive into the stored credential key."""
    from urllib.parse import parse_qs, urlparse

    from starlette.testclient import TestClient

    from rosetta.main import create_app

    monkeypatch.setenv("ROSETTA_AUTH", "oidc")
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/google/enroll",
                       headers=[(b"Remote-User", "Sébastien".encode("utf-8"))],
                       follow_redirects=False)
        assert r.status_code == 302
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]

        def handler(request):
            return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "scope": ""})

        monkeypatch.setattr(google, "_transport", mock(handler))
        r = client.get(f"/google/callback?code=abc&state={state}")
        assert r.status_code == 200 and "Sébastien" in r.text
    stored = json.loads((data_dir / "users" / "S_bastien.json").read_text())
    assert stored["sub"] == "Sébastien"


def test_calendar_create_all_day_vs_datetime(enrolled, monkeypatch):
    captured = {}

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        captured.update(json.loads(request.read()))
        return httpx.Response(200, json={"id": "ev1", "htmlLink": "https://cal"})

    monkeypatch.setattr(google, "_transport", mock(handler))
    out = run(google.calendar_create("Vacances", "2026-08-01", "2026-08-15"))
    assert out["id"] == "ev1"
    assert captured["start"] == {"date": "2026-08-01"}
    run(google.calendar_create("Dîner", "2026-08-01T20:00:00+02:00", "2026-08-01T22:00:00+02:00"))
    assert "dateTime" in captured["start"]


# -- mail_attachment -------------------------------------------------------

def _msg_with_attachment(filename, mime, att_id="att-1", size=10):
    """A format=full message payload carrying one attachment part."""
    return {"payload": {"parts": [
        {"filename": "", "mimeType": "text/plain", "body": {"data": ""}},
        {"filename": filename, "mimeType": mime,
         "body": {"attachmentId": att_id, "size": size}},
    ]}}


def test_mail_attachment_lists(enrolled, monkeypatch):
    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        return httpx.Response(200, json=_msg_with_attachment("avoir.pdf", "application/pdf",
                                                             att_id="att-9", size=12345))

    monkeypatch.setattr(google, "_transport", mock(handler))
    out = run(google.mail_attachment("m1"))
    assert out["attachments"] == [
        {"attachment_id": "att-9", "filename": "avoir.pdf",
         "mime_type": "application/pdf", "size_bytes": 12345},
    ]


def test_mail_attachment_fetches_text(enrolled, monkeypatch):
    payload = base64.urlsafe_b64encode("Montant : 42,00 €".encode()).decode()

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if "/attachments/" in request.url.path:
            return httpx.Response(200, json={"data": payload})
        return httpx.Response(200, json=_msg_with_attachment("note.txt", "text/plain"))

    monkeypatch.setattr(google, "_transport", mock(handler))
    out = run(google.mail_attachment("m1", "att-1"))
    assert out["mime_type"] == "text/plain"
    assert "42,00" in out["text"]
    assert "data_base64" not in out


def test_mail_attachment_pdf_routed_to_extractor(enrolled, monkeypatch):
    raw = b"%PDF-1.4 fake bytes"
    payload = base64.urlsafe_b64encode(raw).decode()
    monkeypatch.setattr(google, "_pdf_to_text", lambda b: f"[{len(b)}o] Total: 42 EUR")

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if "/attachments/" in request.url.path:
            return httpx.Response(200, json={"data": payload})
        return httpx.Response(200, json=_msg_with_attachment("avoir.pdf", "application/pdf"))

    monkeypatch.setattr(google, "_transport", mock(handler))
    out = run(google.mail_attachment("m1", "att-1"))
    assert out["mime_type"] == "application/pdf"
    assert "Total: 42 EUR" in out["text"]


def test_mail_attachment_binary_points_to_raw(enrolled, monkeypatch):
    payload = base64.urlsafe_b64encode(b"\x89PNG\r\n\x1a\n....").decode()

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if "/attachments/" in request.url.path:
            return httpx.Response(200, json={"data": payload})
        return httpx.Response(200, json=_msg_with_attachment("photo.png", "image/png"))

    monkeypatch.setattr(google, "_transport", mock(handler))
    out = run(google.mail_attachment("m1", "att-1"))
    assert "text" not in out
    assert "raw=True" in out["note"] and "image/png" in out["note"]


def test_mail_attachment_raw_returns_native_bytes(enrolled, monkeypatch):
    blob = b"\x89PNG\r\n\x1a\nnative-bytes"
    payload = base64.urlsafe_b64encode(blob).decode()

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if "/attachments/" in request.url.path:
            return httpx.Response(200, json={"data": payload})
        return httpx.Response(200, json=_msg_with_attachment("photo.png", "image/png",
                                                             size=len(blob)))

    monkeypatch.setattr(google, "_transport", mock(handler))
    out = run(google.mail_attachment("m1", "att-1", raw=True))
    assert out["encoding"] == "base64"
    assert base64.b64decode(out["data_base64"]) == blob
    assert "text" not in out


def test_mail_attachment_sniffs_mislabeled_pdf(enrolled, monkeypatch):
    """Senders mislabel PDFs as octet-stream and drop the .pdf extension: the byte
    magic (%PDF-) must still route to text extraction, not the binary fallback."""
    raw = b"%PDF-1.7\n... a real pdf, wrongly typed ..."
    payload = base64.urlsafe_b64encode(raw).decode()
    monkeypatch.setattr(google, "_pdf_to_text", lambda b: "Avoir d'acompte : 42,00 EUR")

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if "/attachments/" in request.url.path:
            return httpx.Response(200, json={"data": payload})
        # mislabeled: octet-stream, and a filename WITHOUT a .pdf extension
        return httpx.Response(200, json=_msg_with_attachment("avoir", "application/octet-stream"))

    monkeypatch.setattr(google, "_transport", mock(handler))
    out = run(google.mail_attachment("m1", "att-1"))
    assert "Avoir d'acompte : 42,00 EUR" in out["text"]
    assert "note" not in out


# ---- mail_thread : une coupure doit se voir, se mesurer et se rattraper -------
#
# Le cas réel (août 2026) : un mail de réservation groupée SNCF coupé à 3000
# caractères, dont la fin — les trajets d'octobre — manquait. Le marqueur ne
# disait ni combien manquait ni comment l'obtenir : l'agent a estimé le seuil au
# jugé et a rapporté une panne au lieu d'une troncature. Ce que ces tests
# verrouillent, c'est donc moins le plafond que sa LISIBILITÉ et son issue.

def _thread_with(bodies, thread_id="t42"):
    def enc(text):
        return base64.urlsafe_b64encode(text.encode()).decode()
    return {"messages": [
        {"id": f"m{i}", "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": "Subject", "value": "Confirmation"},
                        {"name": "From", "value": "sncf@exemple.fr"}],
            "body": {"data": enc(b)},
        }} for i, b in enumerate(bodies)]}


def _thread_handler(bodies):
    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        return httpx.Response(200, json=_thread_with(bodies))
    return handler


def test_thread_truncation_states_both_sizes_and_the_way_out(enrolled, monkeypatch):
    """Un corps coupé annonce ce qu'il rend, ce qu'il y avait, et le paramètre à
    relever. Sans ces trois-là, la troncature est une perte de données déguisée
    en réponse — et c'est exactement ce qui s'est produit."""
    monkeypatch.setattr(google, "_transport", mock(_thread_handler(["A" * 9000])))
    out = run(google.mail_thread("t42"))
    body = out["messages"][0]["body"]
    assert "tronqué" in body
    assert "4000" in body and "9000" in body      # rendu ET total, en clair
    assert "body_limit" in body                   # l'issue est nommée
    assert out["body_limit"] == 4000


def test_thread_body_limit_recovers_the_tail(enrolled, monkeypatch):
    """Le rattrapage promis par le marqueur doit exister pour de vrai."""
    monkeypatch.setattr(google, "_transport", mock(_thread_handler(["A" * 9000])))
    out = run(google.mail_thread("t42", body_limit=20000))
    body = out["messages"][0]["body"]
    assert "tronqué" not in body and len(body) == 9000


def test_thread_body_limit_is_clamped_not_rejected(enrolled, monkeypatch):
    """Une valeur absurde rend le plafond, jamais une erreur : un agent ne doit
    pas avoir à comprendre un refus avant de réessayer."""
    monkeypatch.setattr(google, "_transport", mock(_thread_handler(["A" * 100])))
    assert run(google.mail_thread("t42", body_limit=10_000_000))["body_limit"] \
        == google.THREAD_BODY_LIMIT_MAX
    assert run(google.mail_thread("t42", body_limit=0))["body_limit"] \
        == google.THREAD_BODY_LIMIT


def test_thread_total_budget_shrinks_late_messages_and_says_so(enrolled, monkeypatch):
    """Relever body_limit ne doit pas pouvoir faire exploser la réponse : le
    budget total borne le fil, réduit les derniers messages, et LE DIT."""
    bodies = ["B" * 40000] * 6          # 240 000 > THREAD_TOTAL_BUDGET
    monkeypatch.setattr(google, "_transport", mock(_thread_handler(bodies)))
    out = run(google.mail_thread("t42", body_limit=40000))
    sizes = [len(m["body"]) for m in out["messages"]]
    total = sum(sizes)
    # Le budget n'est pas un maximum absolu : un plancher de 500 caractères par
    # message le déborde volontairement, pour ne JAMAIS rendre un corps vide —
    # un message réduit à rien serait indiscernable d'un message sans contenu.
    # Le contrat est donc : budget + (plancher + marqueur) par message réduit.
    assert total <= google.THREAD_TOTAL_BUDGET + 6 * (500 + 120)
    assert sizes[:3] == [40000] * 3        # les premiers passent en entier
    assert all(s < 700 for s in sizes[3:])  # les suivants sont réduits, pas vides
    assert all(s > 0 for s in sizes)
    assert "tronque" in out and "budget total" in out["tronque"]
    assert len(out["messages"]) == 6                       # aucun message perdu
