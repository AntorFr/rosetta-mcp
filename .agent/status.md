# Status — rosetta-mcp

> MàJ : 2026-07-19

**État :** v0.1.0 prête à publier. Hub MCP modulaire (Starlette + FastMCP) : addons
`maps` (Google Routes/Places/Weather) et `transit` (SNCF + IDFM) portés depuis
agent-pods (bug `_dig`/liste corrigé au passage), montés par chemin en streamable
HTTP stateless. Isolation par addon (import raté → `error` sur /health, le hub
reste debout), auth = resource server OAuth 2.1 : JWT RFC 9068 d'Authelia validés
via JWKS (aud/iss), métadonnées RFC 9728 + WWW-Authenticate sur 401. 10 tests, smoke
local OK. Cible : `rosetta.mcp.berard.me` sur tantive (wildcard DNS déjà en place).

**Prochaines étapes :**
- [ ] Chart `rosetta-mcp` (smart-home-charts, base `common`) + manifeste
      `clusters/tantive/home/mcp/rosetta-mcp-helm.yml` (externalSecrets : clés
      `llm/google-api` + `llm/transports` — vérifier le store tantive)
- [ ] Authelia : `access_token_signed_response_alg` + audience rosetta ; clients
      `client_credentials` par agent (alfred, skippy…)
- [ ] Bascule `.mcp.json` d'Alfred (2 entrées http) + dégraissage agent-gw
      (mcp_servers/ + clés d'env) ; skill/login pour les corps headless (device code)
