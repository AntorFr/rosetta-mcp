"""verbatim addon : la fenêtre glissante, la playlist déguisée en sous-titres,
et la troncature rattrapable.

Les deux pièges reproduits ici ont été relevés sur le service vivant le
2026-08-31 et ne sont PAS des hypothèses : l'URL `vtt` d'une piste automatique
YouTube rend un `#EXTM3U` (une playlist de segments, pas du texte), et les
événements json3 d'une ASR portent `aAppend` pour faire défiler l'écran — les
garder écrit deux fois ce qui a été dit une fois.

Tout est mocké : l'extracteur yt-dlp est injecté (`_extract`), le réseau passe
par httpx.MockTransport. Aucun appel sortant.
"""

import asyncio
import json

import httpx
import pytest

from rosetta.addons import verbatim


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clean():
    verbatim._cache.clear()
    yield
    verbatim._extract = None
    verbatim._transport = None
    verbatim._cache.clear()


JSON3 = json.dumps({"events": [
    {"tStartMs": 0, "dDurationMs": 2510, "segs": [{"utf8": "[Musique]"}]},
    {"tStartMs": 4390, "aAppend": 1, "segs": [{"utf8": "\n"}]},
    {"tStartMs": 4400, "segs": [{"utf8": "Le filtre passe-bas n'est pas là pour couper,"}]},
    {"tStartMs": 6800, "aAppend": 1, "segs": [{"utf8": "\n"}]},
    {"tStartMs": 6900, "segs": [{"utf8": "il est là pour donner une couleur au son."}]},
    {"tStartMs": 12000, "segs": [{"utf8": "La résonance remonte les fréquences juste avant la coupure, et ça s'entend tout de suite."}]},
    {"tStartMs": 30000, "segs": [{"utf8": "Poussée trop loin, elle fait osciller le filtre tout seul."}]},
    {"tStartMs": 60000, "segs": [{"utf8": "Le deuxième bloc, l'enveloppe, décide de ce que le filtre raconte dans le temps."}]},
]}, ensure_ascii=False)

# La fenêtre glissante d'une ASR : chaque cue répète la fin du précédent.
VTT = """WEBVTT
Kind: captions
Language: fr

00:00:01.199 --> 00:00:03.629 align:start position:0%
le filtre passe-bas n'est pas là

00:00:03.629 --> 00:00:06.100 align:start position:0%
le filtre passe-bas n'est pas là pour couper

00:00:06.100 --> 00:00:09.000 align:start position:0%
pour couper il est là pour donner une couleur au son. La résonance remonte les fréquences juste avant la coupure.
"""

PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXTINF:600,
https://timedtext.test/segment-1
#EXTINF:519,
https://timedtext.test/segment-2
"""

INFO = {
    "webpage_url": "https://www.youtube.com/watch?v=AAA",
    "title": "Le moteur audio, expliqué",
    "uploader": "Underscore_",
    "vcodec": "vp9",
    "duration": 1122,
    "upload_date": "20260827",
    "description": "On ouvre le capot.",
    "chapters": [{"start_time": 0, "title": "Le filtre"},
                 {"start_time": 252.4, "title": "La résonance"}],
    "subtitles": {"fr": [{"ext": "json3", "url": "https://timedtext.test/fr.json3"},
                         {"ext": "vtt", "url": "https://timedtext.test/fr.vtt"}]},
    "automatic_captions": {
        "fr": [{"ext": "json3", "url": "https://timedtext.test/auto-fr.json3"},
               {"ext": "vtt", "url": "https://timedtext.test/auto-fr.vtt"}],
        "en": [{"ext": "json3", "url": "https://timedtext.test/auto-en.json3"}],
    },
}


def routes(request: httpx.Request) -> httpx.Response:
    path = str(request.url)
    if path.endswith(".json3"):
        return httpx.Response(200, text=JSON3)
    if path.endswith("auto-fr.vtt"):
        # Le piège : l'URL dit vtt, le corps est une playlist.
        return httpx.Response(200, text=PLAYLIST)
    if path.endswith(".vtt"):
        return httpx.Response(200, text=VTT)
    if "segment-" in path:
        return httpx.Response(200, text=VTT)
    return httpx.Response(404)


def wire(info=None, transport=None):
    verbatim._extract = lambda url: dict(info or INFO)
    verbatim._transport = httpx.MockTransport(transport or routes)


def test_les_sous_titres_manuels_priment_sur_lasr():
    wire()
    out = run(verbatim.verbatim("https://www.youtube.com/watch?v=AAA", langues="fr"))
    assert out["origine"] == "manuel"
    assert out["langue"] == "fr"
    assert out["titre"] == "Le moteur audio, expliqué"
    assert out["publie"] == "2026-08-27"
    assert out["duree"] == "00:18:42"


def test_json3_ne_repete_pas_ce_qui_defile():
    wire()
    out = run(verbatim.verbatim("https://www.youtube.com/watch?v=AAA", langues="fr"))
    texte = " ".join(l["texte"] for l in out["lignes"])
    assert texte.count("pour donner une couleur") == 1
    assert "\n" not in texte


def test_chaque_ligne_porte_lheure_et_le_lien_qui_sy_rend():
    wire()
    out = run(verbatim.verbatim("https://www.youtube.com/watch?v=AAA", langues="fr"))
    ligne = out["lignes"][0]
    assert ligne["a"] == verbatim._stamp(ligne["t"])
    assert ligne["lien"].startswith("https://www.youtube.com/watch?v=AAA")
    seconde = next(l for l in out["lignes"] if l["t"] > 0)
    assert f"t={seconde['t']}s" in seconde["lien"]


def test_un_lien_hors_youtube_prend_le_fragment_de_media():
    assert verbatim._lien("https://podcast.test/47.mp3", 90) == "https://podcast.test/47.mp3#t=90"
    # La seconde zéro est le début du média : rien à viser.
    assert verbatim._lien("https://podcast.test/47.mp3", 0) == "https://podcast.test/47.mp3"


def test_une_piste_vtt_asr_est_deroulee_une_seule_fois():
    dits = verbatim._de_cues(VTT)
    texte = " ".join(t for _, t in dits)
    assert texte.count("passe-bas") == 1
    assert texte.count("pour couper") == 1


def test_une_playlist_deguisee_en_vtt_est_suivie():
    # Sans le suivi, ce qui remonte n'est pas du texte mais un sommaire d'URLs.
    info = dict(INFO, subtitles={})
    verbatim._extract = lambda url: dict(info, subtitles={})
    verbatim._transport = httpx.MockTransport(routes)
    async def go():
        async with verbatim._client() as client:
            return await verbatim._texte(client, "https://timedtext.test/auto-fr.vtt")
    corps = run(go())
    assert corps is not None
    assert "#EXTM3U" not in corps
    assert "passe-bas" in corps


def test_une_troncature_se_voit_se_mesure_et_se_rattrape():
    wire()
    debut = run(verbatim.verbatim("https://www.youtube.com/watch?v=AAA", langues="fr", lignes=1))
    assert debut["rendu"] == 1
    assert debut["total"] > 1
    assert "depuis=1" in debut["tronque"]
    suite = run(verbatim.verbatim("https://www.youtube.com/watch?v=AAA", langues="fr",
                                  depuis=1, lignes=50))
    assert suite["depuis"] == 1
    assert "tronque" not in suite
    assert suite["lignes"][0]["texte"] != debut["lignes"][0]["texte"]


def test_un_media_sans_sous_titres_le_dit_et_garde_sa_fiche():
    verbatim._extract = lambda url: dict(INFO, subtitles={}, automatic_captions={})
    verbatim._transport = httpx.MockTransport(routes)
    out = run(verbatim.verbatim("https://www.youtube.com/watch?v=AAA"))
    assert out["lignes"] == []
    assert out["origine"] is None
    assert "aucun sous-titre publié" in out["avertissement"]
    # La fiche reste : titre, durée et chapitres n'ont pas besoin du texte.
    assert out["titre"] == "Le moteur audio, expliqué"
    assert out["chapitres"][1]["a"] == "00:04:12"


def test_cherche_rend_les_passages_dans_lordre_du_media():
    wire()
    out = run(verbatim.verbatim_cherche("https://www.youtube.com/watch?v=AAA",
                                        "résonance filtre", langues="fr"))
    assert out["passages"]
    assert [p["t"] for p in out["passages"]] == sorted(p["t"] for p in out["passages"])
    assert out["cherche_dans"] > 0
    assert all("t=" in p["lien"] or p["t"] == 0 for p in out["passages"])


def test_cherche_ne_rend_pas_trois_fois_le_meme_moment():
    wire()
    out = run(verbatim.verbatim_cherche("https://www.youtube.com/watch?v=AAA",
                                        "filtre", n=8, langues="fr"))
    rangs = [p["t"] for p in out["passages"]]
    assert len(rangs) == len(set(rangs))


def test_un_sujet_trop_court_est_refuse_plutot_que_tout_rendre():
    wire()
    out = run(verbatim.verbatim_cherche("https://www.youtube.com/watch?v=AAA", "le", langues="fr"))
    assert "error" in out


def test_la_fiche_dit_les_langues_disponibles_sans_une_ligne_de_texte():
    wire()
    out = run(verbatim.verbatim_fiche("https://www.youtube.com/watch?v=AAA"))
    assert out["sous_titres"] == {"manuels": ["fr"], "auto": ["en", "fr"]}
    assert "lignes" not in out
    assert out["description"] == "On ouvre le capot."


def test_une_url_illisible_revient_en_phrase_pas_en_trace():
    def boom(url):
        raise RuntimeError("Unsupported URL: https://exemple.test/x")
    verbatim._extract = boom
    out = run(verbatim.verbatim("https://exemple.test/x"))
    assert "error" in out
    assert "Unsupported URL" in out["error"]


def test_sans_yt_dlp_loutil_explique_ce_qui_manque():
    def absent(url):
        raise ImportError("No module named 'yt_dlp'")
    verbatim._extract = absent
    out = run(verbatim.verbatim("https://www.youtube.com/watch?v=AAA"))
    assert "yt-dlp" in out["error"]


def test_une_langue_absente_se_rabat_en_le_disant():
    wire()
    out = run(verbatim.verbatim("https://www.youtube.com/watch?v=AAA", langues="de"))
    # Pas de piste allemande : plutôt que rien, une piste existante, nommée.
    assert out["langue"] in {"fr", "en"}
    assert out["lignes"]
