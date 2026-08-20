"""postier addon : l'expéditeur gelé, la liste blanche, et le souffle du quota.

Le postier est l'unique capacité d'envoi du hub, et chacun de ses garde-fous
mérite son test : l'expéditeur vient de l'env et d'elle seule (aucun argument
ne l'infléchit), un destinataire hors liste blanche fait échouer TOUT l'envoi
avant le SMTP, le quota horaire est une fenêtre glissante (les envois d'hier ne
comptent plus), et la copie Sent qui échoue est une information — jamais une
raison de prétendre que le mail n'est pas parti, puisqu'il est parti.

Tout est simulé : SMTP et IMAP par des faux enregistreurs, aucun réseau.
"""

import time

import pytest

from rosetta.addons import postier


class FakeSmtp:
    def __init__(self, journal):
        self.journal = journal

    def send_message(self, msg):
        self.journal.append(msg)

    def quit(self):
        pass


class FakeImap:
    def __init__(self, journal):
        self.journal = journal

    def append(self, folder, flags, date, raw):
        self.journal.append(folder)
        return "OK", [b""]

    def logout(self):
        pass


@pytest.fixture()
def guichet(monkeypatch):
    monkeypatch.setenv("POSTIER_FROM", "nestor@example.test")
    monkeypatch.setenv("POSTIER_PASSWORD", "secret")
    # Plus de défaut : sans allowlist déclarée, `postier` refuse TOUT (fail-closed).
    monkeypatch.setenv("POSTIER_ALLOWED", "*@example.test")
    envois, copies = [], []
    postier._smtp_factory = lambda: FakeSmtp(envois)
    postier._imap_factory = lambda: FakeImap(copies)
    postier._sent_at.clear()
    yield envois, copies
    postier._smtp_factory = None
    postier._imap_factory = None
    postier._sent_at.clear()


def test_expediteur_gele_et_copie_sent(guichet):
    envois, copies = guichet
    out = postier.envoyer_mail("sebastien@example.test", "Certificat NAS",
                               "Il expire vendredi.")
    assert out["envoyé"] is True and out["copie"] == "copié dans Sent"
    assert copies == ["Sent"]
    msg = envois[0]
    assert msg["From"] == "nestor@example.test"  # l'env décide, pas l'appelant
    assert msg["To"] == "sebastien@example.test"
    assert "Il expire vendredi." in msg.get_content()


def test_hors_liste_blanche_rien_ne_part(guichet):
    envois, _ = guichet
    out = postier.envoyer_mail("sebastien@example.test, evil@ailleurs.com",
                               "fuite", "…")
    assert isinstance(out, str) and "evil@ailleurs.com" in out
    assert envois == []  # un seul intrus fait échouer TOUT l'envoi


def test_liste_blanche_extensible_par_env(guichet, monkeypatch):
    envois, _ = guichet
    monkeypatch.setenv("POSTIER_ALLOWED", "*@example.test, ecole@ville.fr")
    out = postier.envoyer_mail("ecole@ville.fr", "Absence", "Émilie est malade.")
    assert out["envoyé"] is True and envois


def test_quota_fenetre_glissante(guichet, monkeypatch):
    envois, _ = guichet
    monkeypatch.setenv("POSTIER_MAX_PER_HOUR", "2")
    now = time.time()
    postier._sent_at[:] = [now - 10, now - 20]
    out = postier.envoyer_mail("sebastien@example.test", "un de trop", "…")
    assert isinstance(out, str) and "quota" in out and envois == []
    # les envois d'il y a plus d'une heure ne comptent plus
    postier._sent_at[:] = [now - 4000, now - 20]
    assert postier.envoyer_mail("sebastien@example.test", "ça repart", "…")["envoyé"]


def test_copie_sent_en_echec_reste_une_information(guichet):
    envois, _ = guichet

    def casse():
        raise OSError("imap down")

    postier._imap_factory = casse
    out = postier.envoyer_mail("laurine@example.test", "quand même", "parti !")
    assert out["envoyé"] is True and envois
    assert "échec" in out["copie"]
