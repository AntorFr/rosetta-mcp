# Status — rosetta-mcp

> MàJ : 2026-07-19

**État :** **v0.1.0 EN PROD** sur `https://rosetta.mcp.berard.me` (tantive, chart
`rosetta-mcp` 0.1.0, image GHCR multi-arch). Hub MCP modulaire : addons `maps` +
`transit` montés par chemin (streamable HTTP stateless), isolation par addon
(/health par-addon), clés d'API via `openbao-tantive` (`llm/google-api`,
`llm/transports`). Auth : resource server OAuth 2.1 — JWT RFC 9068 d'Authelia
validés via JWKS, RFC 9728 + WWW-Authenticate sur 401. Client machine
`agent-alfred` (client_credentials, audience rosetta, scope `mcp`) déclaré dans
Authelia. **E2E vérifié** : token → `travel_time` (Rennes→Baden 1 h 31) et
`train_departures` (Vannes, temps réel SNCF). Secret clair du client : en session
seulement — à poser dans alfred-helm.yml à la bascule (sinon régénérer).

**Consommation RÉSOLUE (2026-07-20)** : `rosetta-bridge` (repo agent-pods, stdlib
seule, dans les 2 images du pod alfred) — relais stdio→HTTP avec refresh
client_credentials, secret du client via coffre (`oidc/agent-alfred`) + addon
externalSecrets. Bascule `.mcp.json` faite et vérifiée in situ.

**Prochaines étapes :**
- [ ] Dégraissage agent-gw (0.21.0) : retirer `mcp_servers/` + les 3 clés d'API de
      l'externalSecrets d'alfred-helm.yml, après quelques jours sans accroc
- [ ] Client `agent-skippy` + bridge sur le Mac (sessions Skippy locales)
- [ ] Skill de login device-code (RFC 8628) pour les futurs addons à données utilisateur
      (voie 2 : identité utilisateur en rebond — audience rosetta sur le client `alfred`)
