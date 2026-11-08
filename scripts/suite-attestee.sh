#!/usr/bin/env bash
# La suite déclarée de Rosetta (.sdlc.yaml › tests:), jouée par `attester`.
# Virtualenv neuf, installation, lint, tests avec couverture ; le plancher de
# couverture (pyproject.toml › fail_under) fait échouer le passage. Se joue seul.
set -euo pipefail

cd "$(dirname "$0")/.."
VENV=.venv-suite

rm -rf "$VENV"
python3 -m venv "$VENV"
# shellcheck disable=SC1091
. "$VENV/bin/activate"

pip install --quiet -e '.[dev]'
ruff check .
pytest --cov --junitxml=resultats.xml
