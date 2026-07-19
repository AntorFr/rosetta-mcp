# Status — rosetta-mcp

> MàJ : 2026-07-20

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

**v0.2.0 — addon `google` (phase 1 FAITE, non déployée)** : classe user-data —
politique d'identité par addon (token machine → 403, sub humain requis, propagation
via contextvar TESTÉE à travers la pile stateless), 6 outils validés par l'utilisateur
(mail_search/thread/draft + calendar_events/create/update — ni envoi, ni suppression,
ni labels : la garde EST la surface), credentials Google par `sub` côté serveur
(ROSETTA_GOOGLE_DATA), enrôlement navigateur /google/enroll → consent → /google/callback
(state HMAC, gardé par forwardAuth ingress). 20 tests. Identité v1 choisie : REBOND PWA
(pas de device code) — l'identité vient de la session Authelia de la PWA.

**Prochaines étapes :**
- [ ] **Phase 2 (déploiement google)** : chart rosetta-mcp 0.2.0 (volume /data hostPath,
      ingress /google/enroll+callback avec middleware forwardAuth Authelia tantive) ;
      GESTE UTILISATEUR : ajouter https://rosetta.mcp.berard.me/google/callback aux
      redirect URIs du client OAuth dans la console Google + copier client_secret.json
      sur le volume ; enrôlement (passkey + consent) ; test e2e outillé
- [ ] **Phase 3 (rebond PWA)** : Authelia (client alfred : audience rosetta +
      offline_access + access tokens JWT), agent-gw (refresh token par session, token
      utilisateur injecté aux sessions Claude), rosetta-bridge mode utilisateur ;
      puis dégraissage google des 2 images alfred + avenant D17/D24 côté cerveau
- [ ] Dégraissage agent-gw (0.21.0) : retirer `mcp_servers/` + les 3 clés d'API de
      l'externalSecrets d'alfred-helm.yml, après quelques jours sans accroc
- [ ] Client `agent-skippy` + bridge sur le Mac (sessions Skippy locales)
