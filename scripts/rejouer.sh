#!/usr/bin/env bash
# Rejeu de tests nommés (.sdlc.yaml › tests: › rejouer:, `scripts/rejouer.sh {tests}`).
#
# Rendu de {tests} : des noms de test tels que le rapport JUnit (resultats.xml) les porte,
# un argument par nom (ou plusieurs séparés par des espaces dans un même argument). Chaque
# nom est soit nu (`test_addons_allowlist`), soit préfixé du classname JUnit
# (`tests.test_hub.test_addons_allowlist`, `tests.test_x.TestC.test_m`), avec ou sans
# guillemets simples ou doubles autour. Un nom nu qui désigne plusieurs tests les rejoue tous.
# Un nom qui ne correspond à aucun test fait sortir en erreur (code 2) : jamais un vert
# à zéro test.
#
# Environnement : réutilise le virtualenv .venv-suite de suite-attestee.sh s'il existe ;
# sinon le crée et fait `pip install -e .[dev]` comme elle (le coût d'un venv neuf n'est
# payé qu'au premier rejeu). Aucun pytest préinstallé n'est supposé.
#
# Sortie : le même rapport JUnit, resultats.xml, avec les seuls tests nommés. Ni ruff, ni
# couverture, ni plancher de couverture.
set -euo pipefail

cd "$(dirname "$0")/.."
VENV=.venv-suite

if [ "$#" -eq 0 ]; then
  echo "rejouer.sh : aucun nom de test donné" >&2
  exit 2
fi

if [ ! -x "$VENV/bin/pytest" ]; then
  rm -rf "$VENV"
  python3 -m venv "$VENV"
  # shellcheck disable=SC1091
  . "$VENV/bin/activate"
  pip install --quiet -e '.[dev]'
else
  # shellcheck disable=SC1091
  . "$VENV/bin/activate"
fi

COLLECTE=$(mktemp)
IDS=$(mktemp)
trap 'rm -f "$COLLECTE" "$IDS"' EXIT

pytest --collect-only -q -p no:cacheprovider >"$COLLECTE" 2>&1 || {
  cat "$COLLECTE" >&2
  echo "rejouer.sh : la collecte des tests a échoué" >&2
  exit 2
}

python3 -I - "$COLLECTE" "$IDS" "$@" <<'PY'
import re
import sys

collecte, sortie, *demandes = sys.argv[1:]
noeuds = [ligne.rstrip("\n") for ligne in open(collecte, encoding="utf-8") if "::" in ligne and not ligne.startswith((" ", "="))]


def jalons(noeud):
    """Identifiant JUnit d'un nœud pytest : (classname.name sans paramètres, nom avec paramètres)."""
    base, crochet, params = noeud.partition("[")
    chemin, *reste = base.split("::")
    dotted = ".".join([re.sub(r"\.py$", "", chemin).replace("/", ".")] + reste)
    nom = reste[-1] + (crochet + params if crochet else "")
    return dotted + (crochet + params if crochet else ""), nom


index = [(noeud, *jalons(noeud)) for noeud in noeuds]
noms = []
for demande in demandes:
    morceaux = [demande] if "[" in demande else demande.split()
    noms.extend(morceaux)

choisis, inconnus = [], []
for brut in noms:
    nom = brut.strip().strip("'\"").strip()
    trouves = [n for n, complet, court in index if nom in (complet, court) or complet.endswith("." + nom)]
    if not trouves:
        inconnus.append(brut)
    for n in trouves:
        if n not in choisis:
            choisis.append(n)

if inconnus or not choisis:
    for brut in inconnus:
        print(f"rejouer.sh : aucun test ne porte le nom {brut!r}", file=sys.stderr)
    sys.exit(2)
with open(sortie, "w", encoding="utf-8") as f:
    f.write("\n".join(choisis) + "\n")
PY

mapfile -t NOEUDS <"$IDS"
exec pytest -p no:cacheprovider --junitxml=resultats.xml "${NOEUDS[@]}"
