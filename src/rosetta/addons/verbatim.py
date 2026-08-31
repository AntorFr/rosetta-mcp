"""`verbatim` addon — ce qui a été DIT dans une vidéo ou un épisode, horodaté.

Une fiche de veille écrite depuis la description d'une vidéo est une fiche
écrite depuis l'argumentaire de son auteur. Cet addon rend l'autre chose : le
texte prononcé, découpé en phrases, chacune avec la SECONDE où elle commence —
donc citable, et surtout ouvrable là où ça se passe (`…&t=252s`).

Il ne transcrit rien lui-même : il va chercher les sous-titres **déjà publiés**
par la plateforme, via `yt-dlp` pour la résolution et un GET pour le texte. Pas
de reconnaissance vocale, donc pas de GPU, pas de fichier audio téléchargé, et
un média sans sous-titres publiés revient **vide en le disant** plutôt que
transcrit à peu près. C'est la frontière de l'addon, et elle est nette.

⚠️ **Manuel ou automatique, ce n'est pas la même matière.** Les sous-titres
manuels sont ponctués et relus ; les automatiques sont une ASR sans ponctuation
fiable, qui écorche les noms propres et les chiffres. Chaque réponse porte
`origine` pour cette raison : citer une ASR au mot près comme si l'auteur
l'avait écrite est une citation fausse. Les manuels sont préférés partout où
ils existent.

⚠️ **Le format compte, et pas pour des raisons de goût.** Sur YouTube, l'URL
`vtt` des sous-titres automatiques rend une PLAYLIST HLS (`#EXTM3U`), pas du
VTT — et les cues automatiques défilent en se répétant (fenêtre glissante), si
bien qu'une concaténation naïve triple le texte. Le format `json3` évite les
deux : ses événements de continuation portent `aAppend`, et les ignorer donne
le texte une fois. D'où l'ordre de préférence json3 → vtt → srt, et le suivi de
playlist en repli. Vérifié sur le service vivant le 2026-08-31.

Outils (descriptions en français — voir README) :
  - verbatim         : le transcript horodaté, par tranches rattrapables
  - verbatim_cherche : où un sujet a été abordé, avec le lien qui s'y rend
  - verbatim_fiche   : titre, chaîne, durée, chapitres, langues dispo — sans le texte
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from ._common import TIMEOUT, new_server

logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = new_server("verbatim")

# Rien à déclarer : aucune clé, aucun quota. Une chaîne YouTube et un podcast
# publient leurs sous-titres sans rien demander à personne.
required_env: list[str] = []

# Combien de lignes une réponse rend par défaut, et son plafond. Une heure de
# parole fait ~350 lignes de phrase : le défaut tient dans un contexte, le
# plafond existe pour les demandes délibérées, et la troncature dit toujours
# comment aller chercher la suite.
LIGNES_DEFAUT = 300
LIGNES_MAX = 1200
# La longueur visée d'une ligne, en caractères. Assez pour qu'un extrait soit
# une pensée, assez court pour que l'horodatage désigne encore le bon moment.
GRAIN = 220
# Un transcript ne change pas : le garder une heure évite de re-tirer 400 Ko
# parce qu'on pose une deuxième question sur la même vidéo.
TTL = 3600.0
CACHE_MAX = 24

_CUE = re.compile(r"^(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}\s*-->\s*(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}")
_BALISE = re.compile(r"<[^>]*>")

# Coutures de test, comme les autres addons : les tests injectent l'extracteur
# et un MockTransport httpx. Aucun réseau dans la suite.
_extract = None
_transport = None
_cache: dict[tuple, tuple[float, dict]] = {}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, transport=_transport, follow_redirects=True)


# ── la plateforme ──────────────────────────────────────────────────────────


def _yt_dlp_info(url: str) -> dict:
    """Ce que la plateforme dit du média. Import paresseux : sans `yt-dlp`,
    l'addon reste monté et c'est l'OUTIL qui explique ce qui manque — un addon
    qui refuse de démarrer prive aussi des questions qu'il pouvait traiter."""
    from yt_dlp import YoutubeDL  # noqa: PLC0415 — voir docstring

    with YoutubeDL({
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        "socket_timeout": 20,
    }) as ydl:
        return ydl.extract_info(url, download=False)


async def _info(url: str) -> dict | str:
    """La fiche brute, ou une phrase qui dit pourquoi non."""
    extract = _extract or _yt_dlp_info
    try:
        # `extract_info` est bloquant (réseau + extracteurs) : dans un thread,
        # sinon il fige la boucle du hub — et le hub sert d'autres addons.
        return await asyncio.to_thread(extract, url)
    except ImportError:
        return ("yt-dlp n'est pas installé dans cette image : impossible de lire "
                "les sous-titres publiés. Le reste du hub n'est pas concerné.")
    except Exception as e:  # noqa: BLE001 — yt-dlp lève sa propre famille d'erreurs
        detail = str(e).strip().splitlines()[-1][:300] if str(e).strip() else type(e).__name__
        return f"cette URL n'a pas pu être lue : {detail}"


def _langues(valeur: str) -> list[str]:
    return [tag.strip().lower() for tag in str(valeur or "").split(",") if tag.strip()]


def _piste(info: dict, voulues: list[str]) -> tuple[str | None, str | None, list]:
    """La meilleure piste : manuelle d'abord, dans l'ordre des langues demandées.

    Le repli est explicite plutôt que muet — une piste dans une langue que
    personne n'a demandée vaut mieux que rien, à condition que la réponse dise
    laquelle et d'où elle vient.
    """
    manuels = info.get("subtitles") or {}
    autos = info.get("automatic_captions") or {}

    for origine, table in (("manuel", manuels), ("auto", autos)):
        for voulue in voulues:
            for code, formats in table.items():
                # `fr` doit attraper `fr-FR` et `fr-orig`, jamais `frr`.
                base = str(code).lower().split("-")[0]
                if base == voulue and formats:
                    return code, origine, formats
    for origine, table in (("manuel", manuels), ("auto", autos)):
        for code, formats in table.items():
            if formats:
                return code, origine, formats
    return None, None, []


def _url_format(formats: list) -> tuple[str | None, str | None]:
    """L'URL à tirer, et dans quel format elle est. Ordre de préférence en tête
    de fichier — json3 d'abord, et ce n'est pas une question de goût."""
    for ext in ("json3", "vtt", "srt", "ttml", "srv1"):
        for f in formats:
            if f.get("ext") == ext and f.get("url"):
                return f["url"], ext
    for f in formats:
        if f.get("url"):
            return f["url"], f.get("ext") or "vtt"
    return None, None


async def _texte(client: httpx.AsyncClient, url: str) -> str | None:
    """Le corps des sous-titres — en suivant la playlist quand c'en est une.

    YouTube sert le `vtt` des pistes automatiques sous forme de playlist HLS :
    le corps est un `#EXTM3U` listant des segments. Sans ce suivi, ce qui
    arrive n'est pas du texte, c'est un sommaire d'URLs.
    """
    try:
        r = await client.get(url)
        if r.status_code != 200:
            return None
        body = r.text
    except httpx.HTTPError:
        return None

    if not body.lstrip().startswith("#EXTM3U"):
        return body

    morceaux = []
    for ligne in body.splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#"):
            continue
        try:
            seg = await client.get(ligne)
        except httpx.HTTPError:
            continue
        if seg.status_code == 200:
            morceaux.append(seg.text)
    return "\n".join(morceaux) if morceaux else None


# ── le texte ───────────────────────────────────────────────────────────────


def _de_json3(payload: str) -> list[tuple[int, str]]:
    """Les événements json3, moins ceux qui ne font que faire défiler l'écran."""
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return []
    dits: list[tuple[int, str]] = []
    for event in data.get("events") or []:
        # `aAppend` marque une continuation de la fenêtre affichée : la garder,
        # c'est écrire deux fois ce qui a été dit une fois.
        if event.get("aAppend"):
            continue
        texte = "".join(seg.get("utf8", "") for seg in event.get("segs") or [])
        texte = " ".join(texte.split())
        if texte:
            dits.append((int(event.get("tStartMs", 0)) // 1000, texte))
    return dits


def _secondes(stamp: str) -> int:
    parts = stamp.strip().replace(",", ".").split(":")
    try:
        return int(sum(float(p) * 60 ** i for i, p in enumerate(reversed(parts))))
    except ValueError:
        return 0


def _de_cues(payload: str) -> list[tuple[int, str]]:
    """VTT et SRT — et la fenêtre glissante des sous-titres automatiques.

    Chaque cue d'une piste ASR répète la fin de la précédente pour que le texte
    défile ; on ne garde donc que ce qu'un cue AJOUTE.
    """
    cues: list[tuple[int, str]] = []
    courant: list[str] | None = None
    debut = 0
    for brute in str(payload or "").splitlines():
        ligne = brute.strip()
        if _CUE.match(ligne):
            if courant is not None:
                cues.append((debut, " ".join(courant)))
            debut = _secondes(ligne.split("-->")[0])
            courant = []
            continue
        if courant is None or not ligne:
            continue
        if ligne.upper().startswith("WEBVTT") or ligne.startswith(("Kind:", "Language:")):
            continue
        if ligne.isdigit():
            continue
        propre = " ".join(_BALISE.sub("", ligne).split())
        if propre:
            courant.append(propre)
    if courant:
        cues.append((debut, " ".join(courant)))

    dits: list[tuple[int, str]] = []
    precedent = ""
    for t, texte in cues:
        if not texte:
            continue
        ajout = texte
        if precedent:
            if precedent.endswith(texte) or precedent == texte:
                continue
            taille = min(len(precedent), len(texte))
            for n in range(taille, 8, -1):
                if precedent.endswith(texte[:n]):
                    ajout = texte[n:].strip()
                    break
        if ajout:
            dits.append((t, ajout))
        precedent = texte
    return dits


def _phrases(dits: list[tuple[int, str]], grain: int = GRAIN) -> list[dict]:
    """Des fragments à des phrases : un extrait doit se lire, et son horodatage
    doit désigner le moment où la phrase COMMENCE."""
    lignes: list[dict] = []
    ouvert: dict | None = None
    for t, texte in dits:
        if ouvert is None:
            ouvert = {"t": t, "texte": texte}
        else:
            ouvert["texte"] = f"{ouvert['texte']} {texte}"
        fini = re.search(r"[.?!…]\s*$", ouvert["texte"])
        if len(ouvert["texte"]) >= grain and fini:
            lignes.append(ouvert)
            ouvert = None
        elif len(ouvert["texte"]) >= grain * 1.8:
            lignes.append(ouvert)
            ouvert = None
    if ouvert:
        lignes.append(ouvert)
    return lignes


def _stamp(secondes: int) -> str:
    s = max(0, int(secondes))
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


def _lien(url: str, secondes: int) -> str:
    """Le média, à cette seconde-là. C'est la moitié de l'intérêt d'un
    horodatage : pouvoir y aller."""
    at = max(0, int(secondes))
    if not url or not at:
        return url
    try:
        p = urlparse(url)
    except ValueError:
        return url
    hote = p.hostname.replace("www.", "").lower() if p.hostname else ""
    if hote.endswith("youtube.com") or hote == "youtu.be":
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != "t"]
        q.append(("t", f"{at}s"))
        return urlunparse(p._replace(query=urlencode(q)))
    # Le fragment de média du standard HTML : honoré par n'importe quel lecteur
    # <audio>/<video>, ignoré sans casse ailleurs.
    return urlunparse(p._replace(fragment=f"t={at}"))


def _fiche(info: dict) -> dict:
    chapitres = [
        {"a": _stamp(int(c.get("start_time") or 0)), "t": int(c.get("start_time") or 0),
         "titre": str(c.get("title") or "").strip()}
        for c in (info.get("chapters") or [])
        if str(c.get("title") or "").strip()
    ]
    date = str(info.get("upload_date") or "")
    duree = info.get("duration")
    return {
        "url": info.get("webpage_url") or info.get("original_url"),
        "titre": info.get("title"),
        "source": info.get("uploader") or info.get("channel") or info.get("playlist_title"),
        "media": "video" if (info.get("vcodec") and info.get("vcodec") != "none") else "audio",
        "publie": f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date) == 8 else None,
        "secondes": int(duree) if isinstance(duree, (int, float)) else None,
        "duree": _stamp(int(duree)) if isinstance(duree, (int, float)) else None,
        "chapitres": chapitres,
    }


async def _transcript(url: str, langues: str) -> dict:
    """La fiche et les phrases, mises en cache — ou `error`."""
    voulues = _langues(langues) or ["fr", "en"]
    clef = (url, tuple(voulues))
    hit = _cache.get(clef)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    info = await _info(url)
    if isinstance(info, str):
        return {"error": info}

    code, origine, formats = _piste(info, voulues)
    fiche = _fiche(info)
    fiche["langue"] = code
    fiche["origine"] = origine
    fiche["lignes"] = []

    if not formats:
        fiche["avertissement"] = (
            "aucun sous-titre publié pour ce média — rien n'a été transcrit. La fiche "
            "(titre, durée, chapitres) est là, le texte prononcé ne l'est pas.")
    else:
        piste, ext = _url_format(formats)
        async with _client() as client:
            payload = await _texte(client, piste) if piste else None
        if payload is None:
            fiche["avertissement"] = "les sous-titres existent mais n'ont pas pu être tirés."
        else:
            dits = _de_json3(payload) if ext == "json3" else _de_cues(payload)
            fiche["lignes"] = _phrases(dits)
            if not fiche["lignes"]:
                fiche["avertissement"] = "la piste de sous-titres est vide."

    if len(_cache) >= CACHE_MAX:
        _cache.pop(next(iter(_cache)), None)
    _cache[clef] = (time.monotonic() + TTL, fiche)
    return fiche


def _sans_accents(valeur: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", str(valeur or "").lower())
        if unicodedata.category(c) != "Mn"
    )


# ── les outils ─────────────────────────────────────────────────────────────


@mcp.tool()
async def verbatim(url: str, langues: str = "fr,en", depuis: int = 0,
                   lignes: int = LIGNES_DEFAUT) -> dict:
    """Ce qui a été dit dans une vidéo ou un épisode, en phrases horodatées.

    Va chercher les sous-titres DÉJÀ PUBLIÉS par la plateforme (YouTube et tout
    ce que yt-dlp sait lire). Aucune reconnaissance vocale : un média sans
    sous-titres revient sans texte, et le dit.

    url     : la page du média (`https://www.youtube.com/watch?v=…`, un épisode…).
    langues : préférences, dans l'ordre (« fr,en »). À défaut, une piste
              existante est rendue et `langue` dit laquelle.
    depuis  : rang de la première ligne rendue — c'est ainsi qu'on rattrape la
              suite d'un transcript tronqué.
    lignes  : combien de lignes rendre (max 1200).

    ⚠️ `origine` vaut `manuel` ou `auto`. « auto » = reconnaissance vocale :
    ponctuation approximative, noms propres et chiffres écorchés. Résumer, oui ;
    citer au mot près comme si l'auteur l'avait écrit, non.

    Chaque ligne porte `t` (secondes), `a` (hh:mm:ss) et `lien` — le média à
    cette seconde-là.
    """
    fiche = await _transcript(url, langues)
    if "error" in fiche:
        return fiche

    toutes = fiche.get("lignes") or []
    depuis = max(0, int(depuis or 0))
    combien = max(1, min(int(lignes or LIGNES_DEFAUT), LIGNES_MAX))
    tranche = toutes[depuis:depuis + combien]
    lien_de = fiche.get("url") or url

    out = {k: v for k, v in fiche.items() if k != "lignes"}
    out["lignes"] = [
        {"t": l["t"], "a": _stamp(l["t"]), "texte": l["texte"], "lien": _lien(lien_de, l["t"])}
        for l in tranche
    ]
    out["total"] = len(toutes)
    out["depuis"] = depuis
    out["rendu"] = len(tranche)
    fin = depuis + len(tranche)
    if fin < len(toutes):
        # Mesurée et rattrapable : une coupe muette est une perte de données
        # déguisée en réponse.
        out["tronque"] = (f"{fin} lignes rendues sur {len(toutes)} — rappeler avec "
                          f"depuis={fin} pour la suite.")
    return out


@mcp.tool()
async def verbatim_cherche(url: str, sujet: str, n: int = 8,
                           langues: str = "fr,en") -> dict:
    """Où un sujet a été abordé dans un média — les passages, et le lien qui s'y rend.

    Pour « sors-moi le passage sur … » sans faire passer une heure de parole
    dans le contexte. Rend les passages qui contiennent les mots du sujet, du
    début vers la fin, chacun avec son horodatage et son lien.

    url   : la page du média. sujet : les mots cherchés (accents indifférents).
    n     : combien de passages au plus (défaut 8).

    Ne classe rien par intérêt : il dit où les mots sont. Ce que ça vaut, c'est
    à l'appelant d'en juger — et `origine: auto` reste une ASR, avec ses fautes.
    """
    fiche = await _transcript(url, langues)
    if "error" in fiche:
        return fiche

    toutes = fiche.get("lignes") or []
    mots = [m for m in re.split(r"[^0-9a-zà-ÿ]+", _sans_accents(sujet)) if len(m) > 2]
    if not mots:
        return {"error": "sujet trop court : donne au moins un mot de trois lettres."}

    fenetre = 3
    plies = [_sans_accents(l["texte"]) for l in toutes]
    trouves = []
    for i in range(len(toutes)):
        passage = " ".join(plies[i:i + fenetre])
        touches = [m for m in mots if m in passage]
        if not touches:
            continue
        trouves.append({
            "i": i,
            "score": len(touches) + len(touches) / len(mots),
            "t": toutes[i]["t"],
            "texte": " ".join(l["texte"] for l in toutes[i:i + fenetre]),
        })

    # Deux fenêtres voisines décrivent le même passage : on garde la meilleure
    # de chaque suite plutôt que de rendre trois fois le même moment.
    trouves.sort(key=lambda h: (-h["score"], h["i"]))
    gardes: list[dict] = []
    for h in trouves:
        if any(abs(g["i"] - h["i"]) < fenetre for g in gardes):
            continue
        gardes.append(h)
        if len(gardes) >= max(1, int(n or 8)):
            break
    gardes.sort(key=lambda h: h["i"])

    lien_de = fiche.get("url") or url
    return {
        **{k: v for k, v in fiche.items() if k != "lignes"},
        "sujet": sujet,
        "passages": [
            {"t": g["t"], "a": _stamp(g["t"]), "texte": g["texte"], "lien": _lien(lien_de, g["t"])}
            for g in gardes
        ],
        # Sur quoi la recherche a porté : « rien trouvé » dans un transcript de
        # 0 ligne et dans un transcript de 400 ne veut pas dire la même chose.
        "cherche_dans": len(toutes),
    }


@mcp.tool()
async def verbatim_fiche(url: str) -> dict:
    """Ce que la plateforme dit d'un média — sans une ligne de son texte.

    Titre, chaîne, date, durée, chapitres, et les langues de sous-titres
    disponibles (manuelles et automatiques séparées). À demander avant
    `verbatim` quand la question est « est-ce que ça vaut l'heure ? » plutôt que
    « qu'est-ce qui y est dit ? ».
    """
    info = await _info(url)
    if isinstance(info, str):
        return {"error": info}
    fiche = _fiche(info)
    fiche["sous_titres"] = {
        "manuels": sorted((info.get("subtitles") or {}).keys()),
        "auto": sorted((info.get("automatic_captions") or {}).keys()),
    }
    description = str(info.get("description") or "").strip()
    limite = 1500
    fiche["description"] = (
        description if len(description) <= limite
        else description[:limite] + f"\n[… tronqué : {limite} caractères rendus sur {len(description)}]"
    )
    return fiche
