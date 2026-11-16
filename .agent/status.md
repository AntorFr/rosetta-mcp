# État — rosetta-mcp

## Dernière feature assemblée : `rosetta-telephone-lieux`
- `search_places` (addon `maps`) rend `phone` pour chaque résultat : `internationalPhoneNumber` de Google, ou `None` (clé toujours présente). Décision : ARCHITECTURE.md › AD-telephone-1.
- Reste : rien pour cette feature.

## Pièges encore vrais
- `geocode` ne doit pas demander le téléphone : ses champs sont Pro, y ajouter le champ le ferait facturer au palier Enterprise.
- Après un changement de description d'outil, régénérer `tests/surface.json` (`python tests/test_surface.py --write`).
- Pas de CI GitHub pour les tests : la preuve est l'attestation `tests-complets` (voir CLAUDE.md).
