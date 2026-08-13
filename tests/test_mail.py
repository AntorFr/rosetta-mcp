"""mail addon : le token qui ouvre le coffre, le fil de discussion, l'alias d'autrui.

Quatre vérités structurent ces tests. Le mot de passe ne vient PAS du pod :
l'addon échange le bearer de l'appelant contre un token coffre (auth JWT) et
ne lit que `creds/<mail_local>` — on vérifie que le login part avec le BON
token et le bon rôle, que le refus du coffre rend une erreur explicite, et que
le cache évite un login par appel. L'identité : le claim `mail_local` prime,
le pliage de `preferred_username` (« Sébastien » → sebastien) reste le filet.
Un brouillon de réponse chaîne `In-Reply-To`/`References` et honore `Reply-To`
avant `From`. Et les alias ne se manipulent QUE vers sa propre boîte : la
suppression de l'alias d'autrui échoue par construction, pas par politesse.

Tout est simulé : IMAP par un faux enregistreur, coffre et OVH par
httpx.MockTransport.
"""

import json

import httpx
import pytest

from rosetta.addons import mail
from rosetta.auth import current_claims, current_token


class FakeImap:
    """Enregistre ce que l'addon fait, répond ce qu'on lui a préparé."""

    def __init__(self):
        self.appended = []
        self.search_args = None
        self.search_result = b""
        self.messages = {}  # uid bytes -> (meta, raw)
        self.folders = [b'(\\HasNoChildren) "/" "INBOX"',
                        b'(\\HasNoChildren) "/" "Drafts"']
        self.logged_out = False

    def list(self):
        return "OK", self.folders

    def select(self, folder, readonly=False):
        return ("NO", [b""]) if folder == "Nulle-Part" else ("OK", [b"2"])

    def uid(self, cmd, *args):
        if cmd == "search":
            self.search_args = args
            return "OK", [self.search_result]
        uid = args[0] if isinstance(args[0], bytes) else str(args[0]).encode()
        entry = self.messages.get(uid)
        return "OK", [entry] if entry else [None]

    def append(self, folder, flags, date, raw):
        self.appended.append((folder, flags, raw))
        return "OK", [b""]

    def logout(self):
        self.logged_out = True


ORIGINAL = (b"From: Facteur <facteur@exemple.fr>\r\n"
            b"Reply-To: guichet@exemple.fr\r\n"
            b"Subject: Devis toiture\r\n"
            b"Date: Tue, 12 Aug 2026 10:00:00 +0200\r\n"
            b"Message-ID: <orig-123@exemple.fr>\r\n"
            b"References: <fil-0@exemple.fr>\r\n"
            b"\r\nBonjour, le devis est pret.\r\n")


@pytest.fixture()
def boite(monkeypatch):
    monkeypatch.setenv("MAIL_IMAP_HOST", "imap.test")
    monkeypatch.setenv("MAIL_DOMAIN", "berard.me")
    fake = FakeImap()
    logins = []
    vault_logins = []

    def factory(email_addr, password):
        logins.append((email_addr, password))
        return fake

    def vault_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/login"):
            vault_logins.append(json.loads(request.content))
            return httpx.Response(200, json={"auth": {"client_token": "vtok"}})
        if path.endswith("/creds/sebastien"):
            return httpx.Response(200, json={"data": {"data": {"password": "secret"}}})
        return httpx.Response(403, json={"errors": ["permission denied"]})

    mail._imap_factory = factory
    mail._vault_transport = httpx.MockTransport(vault_handler)
    mail._ovh_cache.clear()
    mail._pw_cache.clear()
    t_claims = current_claims.set({"preferred_username": "Sébastien", "sub": "uuid-x"})
    t_token = current_token.set("jeton-signe-de-sebastien")
    yield fake, logins, vault_logins
    current_claims.reset(t_claims)
    current_token.reset(t_token)
    mail._imap_factory = None
    mail._transport = None
    mail._vault_transport = None
    mail._ovh_cache.clear()
    mail._pw_cache.clear()


def test_identite_accentuee_ouvre_le_coffre_puis_la_boite(boite):
    fake, logins, vault_logins = boite
    assert mail.mail_dossiers() == ["INBOX", "Drafts"]
    # le login coffre part avec LE token de l'appelant et le bon rôle
    assert vault_logins == [{"role": "rosetta-mail", "jwt": "jeton-signe-de-sebastien"}]
    assert logins == [("sebastien@berard.me", "secret")]
    assert fake.logged_out


def test_claim_mail_local_prime_sur_le_pliage(boite):
    _, logins, _ = boite
    token = current_claims.set({"preferred_username": "N'Importe Qui",
                                "mail_local": "sebastien", "sub": "uuid-x"})
    try:
        mail.mail_dossiers()
    finally:
        current_claims.reset(token)
    assert logins[-1][0] == "sebastien@berard.me"


def test_cache_evite_un_login_coffre_par_appel(boite):
    _, _, vault_logins = boite
    mail.mail_dossiers()
    mail.mail_dossiers()
    assert len(vault_logins) == 1  # le second appel vit sur le cache


def test_coffre_refuse_l_identite(boite):
    token = current_claims.set({"mail_local": "emilie", "sub": "uuid-e"})
    try:
        message = mail.mail_dossiers()
    finally:
        current_claims.reset(token)
    # le faux coffre ne sert que creds/sebastien : emilie prend un 403 explicite
    assert isinstance(message, str) and "creds/emilie" in message


def test_hors_contexte_utilisateur(boite):
    token = current_claims.set(None)
    try:
        assert "identité introuvable" in mail.mail_dossiers()
    finally:
        current_claims.reset(token)


def test_recherche_criteres_et_resume(boite):
    fake, _, _ = boite
    fake.search_result = b"3 7"
    fake.messages[b"7"] = (
        b"1 (UID 7 FLAGS (\\Flagged) BODY[HEADER.FIELDS (FROM SUBJECT DATE)]",
        ORIGINAL)
    fake.messages[b"3"] = (
        b"2 (UID 3 FLAGS (\\Seen) BODY[HEADER.FIELDS (FROM SUBJECT DATE)]",
        b"From: a@b.c\r\nSubject: Lu\r\n\r\n")
    rows = mail.mail_recherche(de="facteur", non_lus=True, depuis_jours=7)
    flat = " ".join(str(a) for a in fake.search_args)
    assert "FROM" in flat and "UNSEEN" in flat and "SINCE" in flat
    # plus récents d'abord : uid 7 (jamais \Seen) puis 3 (lu)
    assert [r["uid"] for r in rows] == ["7", "3"]
    assert rows[0]["non_lu"] is True and rows[1]["non_lu"] is False
    assert rows[0]["sujet"] == "Devis toiture"


def test_lire_rend_le_corps_sans_marquer(boite):
    fake, _, _ = boite
    fake.messages[b"7"] = (b"1 (UID 7 FLAGS ()", ORIGINAL)
    msg = mail.mail_lire("7")
    assert msg["corps"].startswith("Bonjour")
    assert msg["message_id"] == "<orig-123@exemple.fr>"
    assert msg["pieces_jointes"] == []


def test_brouillon_reponse_chaine_le_fil(boite):
    fake, _, _ = boite
    fake.messages[b"7"] = (b"1 (UID 7 FLAGS ()", ORIGINAL)
    out = mail.mail_brouillon(corps="On signe.", en_reponse_a="7")
    assert out["dossier"] == "Drafts" and out["de"] == "sebastien@berard.me"
    folder, flags, raw = fake.appended[0]
    assert folder == "Drafts" and "Draft" in flags
    import email as email_lib
    draft = email_lib.message_from_bytes(raw)
    # Reply-To prime sur From ; le fil est chaîné ; le sujet devient Re:.
    assert draft["To"] == "guichet@exemple.fr"
    assert draft["In-Reply-To"] == "<orig-123@exemple.fr>"
    assert draft["References"].split() == ["<fil-0@exemple.fr>", "<orig-123@exemple.fr>"]
    assert draft["Subject"] == "Re: Devis toiture"
    assert draft["From"] == "sebastien@berard.me"


def test_brouillon_sans_destinataire(boite):
    assert "aucun destinataire" in mail.mail_brouillon(corps="perdu")


# ---- alias : le périmètre est l'appelant, pas la plateforme ------------------

@pytest.fixture()
def ovh(boite, monkeypatch):
    for key in ("OVH_APPLICATION_KEY", "OVH_APPLICATION_SECRET", "OVH_CONSUMER_KEY"):
        monkeypatch.setenv(key, "k")
    posted, deleted = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/1.0/auth/time":
            return httpx.Response(200, text="1000")
        if path == "/v2/zimbra/platform":
            return httpx.Response(200, json=[{"id": "P1"}])
        if path.endswith("/account"):
            return httpx.Response(200, json=[
                {"id": "A-seb", "currentState": {"email": "sebastien@berard.me"}},
                {"id": "A-lau", "currentState": {"email": "laurine@berard.me"}}])
        if path.endswith("/alias") and request.method == "GET":
            return httpx.Response(200, json=[
                {"id": "AL1", "resourceStatus": "READY", "currentState": {
                    "alias": {"name": "temu@berard.me"}, "target": {"id": "A-seb"}}},
                {"id": "AL2", "resourceStatus": "READY", "currentState": {
                    "alias": {"name": "lau-shop@berard.me"}, "target": {"id": "A-lau"}}}])
        if path.endswith("/alias") and request.method == "POST":
            posted.append(json.loads(request.content))
            return httpx.Response(202, json={"id": "NEW"})
        if "/alias/" in path and request.method == "DELETE":
            deleted.append(path.rsplit("/", 1)[1])
            return httpx.Response(204)
        return httpx.Response(404, json={})

    mail._transport = httpx.MockTransport(handler)
    return posted, deleted


def test_alias_liste_ne_montre_que_les_siens(ovh):
    rows = mail.mail_alias_liste()
    assert [r["alias"] for r in rows] == ["temu@berard.me"]


def test_alias_creer_cible_sa_propre_boite(ovh):
    posted, _ = ovh
    out = mail.mail_alias_creer("wish")
    assert out["alias"] == "wish@berard.me" and out["vers"] == "sebastien@berard.me"
    assert posted[0]["targetSpec"]["targetId"] == "A-seb"


def test_alias_nom_fantaisiste_refuse(ovh):
    assert "invalide" in mail.mail_alias_creer("Père Noël")


def test_alias_supprimer_refuse_celui_d_autrui(ovh):
    _, deleted = ovh
    assert "aucun alias" in mail.mail_alias_supprimer("lau-shop")
    assert deleted == []


def test_alias_supprimer_le_sien(ovh):
    _, deleted = ovh
    assert mail.mail_alias_supprimer("temu")["etat"] == "supprimé"
    assert deleted == ["AL1"]
