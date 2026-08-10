"""vin addon: the LWIN traps that fail silently — a base that carries no
diacritics, a pack size inserted in the MIDDLE of a LWIN18, five zero-padded
digits of millilitres, and a spreadsheet that eats the leading zero of a code —
plus the snapshot honesty (absent ≠ inexistent) and the read-only, network-free
surface. Everything runs off a CSV written by the test: no file to download, no
network, no fixture checked in."""

import asyncio

import pytest

from rosetta.addons import vin


def run(coro):
    return asyncio.run(coro)


# Real rows, trimmed to the columns that matter. Note the header spelling: the
# Liv-ex export is SCREAMING_SNAKE, and `_header_map` must fold it.
HEADER = ("LWIN,DISPLAY_NAME,PRODUCER_TITLE,PRODUCER_NAME,WINE,COUNTRY,REGION,"
          "SUB_REGION,SITE,PARCEL,COLOUR,TYPE,SUB_TYPE,DESIGNATION,"
          "CLASSIFICATION,VINTAGE_CONFIG,FIRST_VINTAGE,FINAL_VINTAGE,STATUS")

ROWS = [
    # The wine of the published worked example (see traps 2 and 3).
    "1012361,Chateau Leoville Barton,Chateau,Leoville Barton,,France,Bordeaux,"
    "Saint-Julien,,,Red,Wine,Still,AOP,2eme Cru Classe,Sequential,1900,,Live",
    # Diacritics: the base spells it "Prum" and "Spatlese" (LWIN guide v1.2).
    "1090699,Joh Jos Prum Wehlener Sonnenuhr Riesling Spatlese Mosel,,"
    "Joh Jos Prum,Wehlener Sonnenuhr Riesling Spatlese,Germany,Mosel,,"
    "Wehlener Sonnenuhr,,White,Wine,Still,,,Sequential,1920,,Live",
    "1120671,Muga Prado Enea Gran Reserva Rioja,,Muga,Prado Enea Gran Reserva,"
    "Spain,Rioja,,,,Red,Wine,Still,DOCa,,Sequential,1964,,Live",
    # Six digits: what an Excel-to-CSV export leaves of "0123456".
    "123456,Domaine de Test Cuvee Zero,Domaine,de Test,Cuvee Zero,France,"
    "Loire,,,,White,Wine,Still,AOP,,SingleVintageOnly,2018,2018,Live",
    # A wine that stopped being made: bounds the vintage check on both sides.
    "1055555,Chateau Fini,Chateau,Fini,,France,Bordeaux,,,,Red,Wine,Still,AOP,,"
    "Sequential,1980,1995,Deleted",
    # Eleven digits: not a LWIN7 row. Counted as rejected, never truncated.
    "10123612009,Ligne LWIN11 egaree,,,,,,,,,,,,,,,,,",
]


@pytest.fixture
def base(tmp_path, monkeypatch):
    """A CSV base wired through the environment, with its mirror in tmp_path."""
    source = tmp_path / "LWINdatabase.csv"
    source.write_text("\n".join([HEADER, *ROWS]) + "\n", encoding="utf-8")
    monkeypatch.setenv("LWIN_DB", str(source))
    monkeypatch.setenv("LWIN_CACHE", str(tmp_path / "mirror.sqlite"))
    return source


# -- folding: the trap that silently finds nothing ---------------------------

def test_fold_strips_what_lwin_does_not_carry():
    # The LWIN guide names these explicitly as unsupported in the base.
    assert vin.fold("Château Léoville") == "chateau leoville"
    assert vin.fold("Spätlese") == "spatlese"
    assert vin.fold("Bruno Paillard, Nec Plus Ultra") == "bruno paillard nec plus ultra"
    # NFKD leaves these standing; the explicit table is the whole point.
    assert vin.fold("Ø") == "o"
    assert vin.fold("Weißburgunder") == "weissburgunder"
    assert vin.fold("Cœur") == "coeur"
    # Apostrophes and hyphens flatten to spaces, so "Clos d'Ora" meets "Clos d Ora".
    assert vin.fold("Château d'Yquem") == "chateau d yquem"
    assert vin.fold("Saint-Julien") == "saint julien"


def test_search_is_accent_blind_in_both_directions(base):
    accented = run(vin.vin_recherche("Château Léoville Barton"))
    plain = run(vin.vin_recherche("chateau leoville barton"))
    assert [r["lwin7"] for r in accented["resultats"]] == ["1012361"]
    assert accented["resultats"] == plain["resultats"]
    # The user types the label; the base spells it without the umlaut.
    prum = run(vin.vin_recherche("Prüm Wehlener Sonnenuhr Spätlese"))
    assert prum["resultats"][0]["lwin7"] == "1090699"


# -- reading the source ------------------------------------------------------

def test_header_variants_land_on_the_same_field():
    mapped = vin._header_map(["LWIN", "Display Name", "producerName", "sub_region",
                              "VINTAGE_CONFIG", "colonne inconnue"])
    assert mapped == {0: "lwin", 1: "display_name", 2: "producer_name",
                      3: "sub_region", 4: "vintage_config"}


def test_a_six_digit_code_is_re_padded_not_rejected():
    # Trap 3 upstream: a spreadsheet drops the leading zero of the whole column.
    assert vin._normalise_lwin("123456") == "0123456"
    assert vin._normalise_lwin(1012361) == "1012361"
    assert vin._normalise_lwin("") is None
    # Longer than 7 is not a LWIN7 row: rejected, never cut down to a wrong key.
    assert vin._normalise_lwin("10123612009") is None


def test_base_reports_what_it_kept_and_what_it_dropped(base):
    state = run(vin.vin_base())
    assert state["prete"] is True
    assert state["vins"] == 5
    assert state["lignes_ecartees"] == 1  # the stray LWIN11 row
    assert state["source"] == str(base)
    assert run(vin.vin_recherche("Cuvee Zero"))["resultats"][0]["lwin7"] == "0123456"


def test_an_unreadable_header_is_an_answer_not_a_crash(tmp_path, monkeypatch):
    source = tmp_path / "autre.csv"
    source.write_text("colonne,autre\n1,2\n", encoding="utf-8")
    monkeypatch.setenv("LWIN_DB", str(source))
    monkeypatch.setenv("LWIN_CACHE", str(tmp_path / "mirror.sqlite"))
    out = run(vin.vin_base())
    assert out["prete"] is False
    assert "colonne LWIN" in out["detail"]


def test_the_mirror_is_rebuilt_when_the_source_moves(base, monkeypatch):
    assert run(vin.vin_base())["vins"] == 5
    base.write_text("\n".join([HEADER, *ROWS,
                               "1999999,Chateau Nouveau,,Nouveau,,France,Bordeaux,"
                               ",,,Red,Wine,Still,AOP,,Sequential,2020,,Live"]) + "\n",
                    encoding="utf-8")
    # mtime granularity: make the stamp unambiguously different.
    import os
    st = base.stat()
    os.utime(base, (st.st_atime, st.st_mtime + 10))
    assert run(vin.vin_base())["vins"] == 6
    assert run(vin.vin_recherche("Chateau Nouveau"))["resultats"][0]["lwin7"] == "1999999"


# -- decoding: LWIN18 is not LWIN16 + 2 --------------------------------------

def test_decode_the_published_worked_examples(base):
    # Liv-ex, Leoville Barton 2009: 75 cl, then a case of 12.
    bottle = run(vin.vin_lwin("1012361200900750"))
    assert bottle["lwin7"] == "1012361"
    assert bottle["millesime"] == "2009"
    assert bottle["contenant_ml"] == 750
    assert bottle["format"] == "bouteille"
    assert "bouteilles" not in bottle
    assert bottle["trouve"] is True
    assert bottle["nom"] == "Chateau Leoville Barton"

    case = run(vin.vin_lwin("101236120091200750"))
    assert case["lwin7"] == "1012361"
    assert case["millesime"] == "2009"
    # Trap 2: the pack is read from the MIDDLE, the size from the tail.
    assert case["bouteilles"] == 12
    assert case["contenant_ml"] == 750


def test_a_lwin18_is_not_a_lwin16_with_two_digits_appended(base):
    wrong = run(vin.vin_lwin("1012361" + "2009" + "00750" + "12"))
    right = run(vin.vin_lwin("1012361" + "2009" + "12" + "00750"))
    # Appending at the end parses as a 7 litre bottle in a case of 75: a
    # perfectly well-formed code naming a bottle that does not exist.
    assert wrong["contenant_ml"] == 75012
    assert wrong["bouteilles"] == 0
    assert right["contenant_ml"] == 750 and right["bouteilles"] == 12


def test_decode_rejects_any_other_length(base):
    out = run(vin.vin_lwin("101236120"))
    assert "error" in out and "9 chiffres" in out["error"]
    assert "error" in run(vin.vin_lwin(""))


def test_a_magnum_and_the_regional_split_above_it(base):
    assert run(vin.vin_lwin("1012361201501500"))["format"] == "magnum"
    # 3 litres has two names depending on the region; both are given.
    trois = run(vin.vin_lwin("1012361201503000"))["format"]
    assert "double-magnum" in trois and "jéroboam" in trois


def test_an_unknown_lwin7_still_decodes_and_says_so(base):
    out = run(vin.vin_lwin("9999999" + "2015"))
    assert out["trouve"] is False
    assert out["millesime"] == "2015"
    assert "structurellement valide" in out["detail"]


# -- composing ---------------------------------------------------------------

def test_compose_pads_the_bottle_size_to_five_digits(base):
    out = run(vin.vin_code("1012361", millesime=2009, contenant_ml=750, bouteilles=12))
    assert out["lwin11"] == "10123612009"
    assert out["lwin16"] == "1012361200900750"
    assert out["lwin18"] == "101236120091200750"
    assert out["connu"] is True
    assert out["producteur"] == "Chateau Leoville Barton"
    # Round trip: what we compose is what the decoder reads back.
    assert run(vin.vin_lwin(out["lwin18"]))["contenant_ml"] == 750


def test_compose_refuses_a_code_it_cannot_form(base):
    # A LWIN16 carries the vintage; a LWIN18 carries the size. Neither can be
    # skipped, and the error says which one is missing.
    assert "millesime" in run(vin.vin_code("1012361", contenant_ml=750))["error"]
    assert "contenant_ml" in run(
        vin.vin_code("1012361", millesime=2009, bouteilles=12))["error"]
    assert "1 à 99" in run(vin.vin_code("1012361", millesime=2009,
                                        contenant_ml=750, bouteilles=120))["error"]
    assert "LWIN7" in run(vin.vin_code("10123612009"))["error"]


def test_composing_for_an_unknown_wine_flags_it_without_refusing(base):
    out = run(vin.vin_code("9999999", millesime=2015))
    assert out["lwin11"] == "99999992015"
    assert out["connu"] is False


# -- the vintage is a caution, never a verdict -------------------------------

def test_a_doubtful_vintage_is_flagged_on_both_bounds(base):
    trop_tot = run(vin.vin_code("1055555", millesime=1970))
    assert "premier millésime" in trop_tot["millesime_douteux"]
    trop_tard = run(vin.vin_code("1055555", millesime=2015))
    assert "dernier millésime" in trop_tard["millesime_douteux"]
    # In range: nothing to say, and the code is composed all the same.
    assert "millesime_douteux" not in run(vin.vin_code("1055555", millesime=1990))


def test_single_vintage_only_warns_rather_than_inventing_nv(base):
    out = run(vin.vin_code("0123456", millesime=2018))
    assert out["lwin11"] == "01234562018"
    assert "Liv-ex" in out["millesime_douteux"]


def test_search_can_carry_the_vintage_through(base):
    out = run(vin.vin_recherche("Leoville Barton", millesime=2009))
    assert out["resultats"][0]["lwin11"] == "10123612009"
    assert "error" in run(vin.vin_recherche("Leoville", millesime=99))


# -- filters, ranking, truncation --------------------------------------------

def test_filters_are_folded_like_everything_else(base):
    assert len(run(vin.vin_recherche("chateau", pays="France"))["resultats"]) == 2
    assert run(vin.vin_recherche("chateau", pays="Espagne"))["resultats"] == []
    assert len(run(vin.vin_recherche("wine", couleur="Red"))["resultats"]) == 3


def test_a_whole_word_outranks_a_word_that_merely_starts_with_it(tmp_path, monkeypatch):
    source = tmp_path / "b.csv"
    source.write_text("\n".join([
        HEADER,
        "1000001,Bartonia Estate,,Bartonia,,France,,,,,Red,Wine,Still,,,Sequential,,,Live",
        "1000002,Chateau Leoville Barton,,Leoville Barton,,France,,,,,Red,Wine,"
        "Still,,,Sequential,,,Live",
    ]) + "\n", encoding="utf-8")
    monkeypatch.setenv("LWIN_DB", str(source))
    monkeypatch.setenv("LWIN_CACHE", str(tmp_path / "m.sqlite"))
    out = run(vin.vin_recherche("barton"))
    assert [r["lwin7"] for r in out["resultats"]] == ["1000002", "1000001"]


def test_a_truncated_search_says_so_instead_of_pretending(base, monkeypatch):
    monkeypatch.setattr(vin, "MAX_MATCHES", 1)
    out = run(vin.vin_recherche("wine"))
    assert out["tronque"] is True
    assert out["total"] == 5
    assert "Préciser la recherche" in out["note"]
    # Under the cap, no flag and no invented total.
    assert "tronque" not in run(vin.vin_recherche("Muga"))


def test_max_results_is_capped(base):
    assert len(run(vin.vin_recherche("wine", max_results=999))["resultats"]) <= vin.MAX_RESULTS
    assert "error" in run(vin.vin_recherche("   "))


# -- no base at all ----------------------------------------------------------

def test_without_a_base_every_tool_answers_in_french(monkeypatch):
    monkeypatch.delenv("LWIN_DB", raising=False)
    assert "non configurée" in run(vin.vin_recherche("leoville"))["error"]
    assert run(vin.vin_base())["prete"] is False
    # Decoding is pure arithmetic: it keeps working, and says what it lacks.
    out = run(vin.vin_lwin("101236120091200750"))
    assert out["contenant_ml"] == 750 and out["bouteilles"] == 12
    assert "non configurée" in out["note"]
    assert run(vin.vin_code("1012361", millesime=2009))["lwin11"] == "10123612009"


def test_a_missing_file_names_the_path_it_looked_at(tmp_path, monkeypatch):
    monkeypatch.setenv("LWIN_DB", str(tmp_path / "absent.csv"))
    out = run(vin.vin_base())
    assert out["prete"] is False
    assert "absent.csv" in out["detail"]
    assert out["telechargement"] == vin.DOWNLOAD_URL


def test_the_addon_declares_its_env_and_carries_no_network_client():
    assert vin.required_env == ["LWIN_DB"]
    # The surface is offline by construction: no HTTP client anywhere in the
    # module, so there is nothing to rate-limit and nothing to leak.
    assert not hasattr(vin, "httpx")
