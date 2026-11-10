# Rosetta — règles pour qui travaille dans ce dépôt

## La preuve de test

- La preuve d'un commit est son attestation `tests-complets`, posée par `attester --publier`,
  au poste ou dans un pod de la chaîne. Sans cette attestation, un commit n'est pas prouvé.
- Aucune CI GitHub ne joue la suite (ni lint, ni tests), sur pull request ou ailleurs.
- L'image ne se construit que sur un tag `v*`, après vérification de l'attestation du commit tagué.
- `attester` est une commande de la chaîne, absente de ce dépôt : on ne la réimplémente pas ici.
- La suite est déclarée dans `.sdlc.yaml` (`tests:`) ; `scripts/suite-attestee.sh` se joue seul
  (virtualenv neuf, `pip install -e .[dev]`, `ruff check .`, `pytest --cov --junitxml=resultats.xml`).
  Le plancher de couverture reste dans `pyproject.toml`.
