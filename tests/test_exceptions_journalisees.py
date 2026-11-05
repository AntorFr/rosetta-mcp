"""Exceptions avalées : journalisées en debug, sortie inchangée (AD-nettoyage-3)."""

import logging

from rosetta.addons import _common, courrier, marees, withings


def test_remote_user_non_latin1_journalise_sans_fuite(caplog):
    from starlette.datastructures import Headers

    class Req:
        headers = Headers({"Remote-User": "Zoé"})  # déjà décodé : l'aller-retour latin-1 échoue

    with caplog.at_level(logging.DEBUG, logger="rosetta.common"):
        out = _common.remote_user(Req())
    assert out == "Zoé"
    assert any(r.levelno == logging.DEBUG for r in caplog.records)
    assert "Zoé" not in caplog.text  # le nom n'est pas journalisé


def test_courrier_date_illisible_gardee_brute(caplog):
    import email.message

    msg = email.message.Message()
    msg["Date"] = "pas une date"
    with caplog.at_level(logging.DEBUG, logger="rosetta.courrier"):
        out = courrier._summary("1", msg, "")
    assert out["date"] == "pas une date"
    assert "pas une date" in caplog.text


def test_marees_hauteur_illisible_champ_omis(caplog):
    with caplog.at_level(logging.DEBUG, logger="rosetta.marees"):
        out = marees._range({"hauteur": "xx", "height": "xx", "hauteur_m": "xx"})
    assert "hauteur_m" not in out


def test_withings_fuseau_inconnu_journalise(caplog):
    with caplog.at_level(logging.DEBUG, logger="rosetta.withings"):
        out = withings._local(0, "Nope/Zone")
    assert out.startswith("1970-01-01")
    assert "Nope/Zone" in caplog.text
