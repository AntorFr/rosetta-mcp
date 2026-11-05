"""current_subject (famille « sujet courant ») : clé du magasin, un octet près (P-3)."""
import unicodedata

from rosetta.addons import _common, github, quotas, withings
from rosetta.auth import current_claims


def _with(claims):
    return current_claims.set(claims)


def test_aucun_claim_donne_none():
    tok = _with(None)
    try:
        assert _common.current_subject() is None
        assert _common.current_subject.__module__ == _common.__name__
    finally:
        current_claims.reset(tok)


def test_preferred_username_prime_et_nfc():
    decomp = unicodedata.normalize("NFD", "Sébastien")
    tok = _with({"preferred_username": decomp, "sub": "opaque-123"})
    try:
        out = _common.current_subject()
        assert out == unicodedata.normalize("NFC", "Sébastien")
        assert out != decomp
    finally:
        current_claims.reset(tok)


def test_repli_sur_sub_et_valeurs_vides():
    for claims, attendu in [
        ({"sub": "abc"}, "abc"),
        ({"preferred_username": "", "sub": "abc"}, "abc"),
        ({"preferred_username": "", "sub": ""}, None),
        ({"other": 1}, None),
    ]:
        tok = _with(claims)
        try:
            assert _common.current_subject() == attendu
        finally:
            current_claims.reset(tok)


def test_alias_des_addons():
    assert github._current_sub is _common.current_subject
    assert quotas._current_sub is _common.current_subject
    assert withings._current_sub is _common.current_subject
    assert not hasattr(withings, "_remote_user")
    assert withings.remote_user is _common.remote_user
