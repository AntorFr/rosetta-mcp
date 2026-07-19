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

**Prochaines étapes :**
- [ ] Consommation par les corps : les tokens expirent (~1 h) et `.mcp.json` a des
      headers statiques → trancher (wrapper stdio→http avec refresh client_credentials
      dans les images, OAuth natif Claude Code pour les corps interactifs, ou lifetime
      allongé sur le client) puis basculer le `.mcp.json` d'Alfred
- [ ] Dégraissage agent-gw : retirer `mcp_servers/` + les 3 clés d'API de l'env
      (redémarrage du pod alfred à coordonner) ; clients `agent-skippy`/autres au besoin
- [ ] Skill de login device-code (RFC 8628) pour les futurs addons à données utilisateur
