# Status — rosetta-mcp

> MàJ : 2026-07-28

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

**v0.2.1 — addon `google` DÉPLOYÉ (phase 2 infra faite, 2026-07-20)** : classe user-data —
politique d'identité par addon (token machine → 403, sub humain requis, propagation
via contextvar TESTÉE à travers la pile stateless), 6 outils validés par l'utilisateur
(mail_search/thread/draft + calendar_events/create/update — ni envoi, ni suppression,
ni labels : la garde EST la surface), credentials Google par `sub` côté serveur
(ROSETTA_GOOGLE_DATA), enrôlement navigateur /google/enroll → consent → /google/callback
(state HMAC, gardé par forwardAuth ingress). 20 tests. Identité v1 choisie : REBOND PWA
(pas de device code) — l'identité vient de la session Authelia de la PWA.

**v0.3.0 — 7e outil `mail_attachment` (2026-07-21, DÉPLOYÉ tantive)** :
lecture des pièces jointes, surface étendue 6→7 (changement délibéré, canari
`test_no_send_tool_exists` mis à jour). Deux modes : transcription TEXTE par défaut
(texte/CSV/JSON + PDF via `pypdf`, octets bruts jamais renvoyés) et `raw=True` qui
rapatrie les octets natifs en base64 (plafond 10 Mio) pour stockage mémoire. Reste
lecture pure — `gmail.readonly` déjà accordé, aucun nouveau scope, aucune écriture.
Nouvelle dep : `pypdf>=5`.

**v0.3.1 (2026-07-21)** : correctif — la détection de type ne se fie plus à l'étiquette
MIME de Gmail (les expéditeurs mettent souvent `application/octet-stream` + un nom sans
`.pdf`, ce qui envoyait le PDF en branche « binaire non transcrit » sans jamais appeler
pypdf). Sniff du magic `%PDF-` sur les octets → un PDF mal étiqueté est désormais
transcrit. Prouvé bout-en-bout sur un vrai PDF. 29 tests.

**v0.4.0 — le brouillon devient modifiable (2026-07-28)** : Alfred savait déposer un
brouillon mais plus jamais le retrouver ni le corriger. Surface 7→9 outils (canari
`test_no_send_tool_exists` mis à jour) : `mail_drafts` (liste les brouillons en attente,
ou en relit un en entier — c'est ce qui permet de retrouver un brouillon d'une session
précédente) et `mail_draft_update` (sémantique PATCH : seuls les champs fournis
changent). **Aucun nouveau scope** — `gmail.compose`, déjà accordé, couvre
drafts.list/get/update : pas de ré-enrôlement. Toujours ni envoi ni suppression.
Piège traité : `drafts.update` est un PUT qui REMPLACE le brouillon → on relit et on
refusionne d'abord, sinon le `threadId` saute et le brouillon se détache de son fil.
Les 3 outils rendent un `link` vers le brouillon dans Gmail — l'URL porte l'id du
MESSAGE (hex), pas le `draft_id` (`r-84…`), et le compte est adressé par son adresse
(`/mail/u/<email>/`) pour ne pas dépendre de l'ordre des comptes du navigateur.
34 tests.

**Dérive amont rattrapée au passage (2026-07-28)** : `mcp` 2.0.0 est sorti et le pin
`mcp>=1.2` l'attrapait — la 2.x déplace `mcp.server.fastmcp`, importé par *tous* les
addons via `_common`. La CI de v0.4.0 a cassé à la collecte ; sans ce gate, l'image
publiée aurait crashé au démarrage. Plafonné à `mcp>=1.2,<2` (CI → 1.29.0, module
vérifié présent dans la wheel). Monter en 2.x = porter les addons, chantier à part.

**v0.4.1 (2026-07-28)** : `/health` mentait — `__version__` était écrit en dur dans
`__init__.py`, jamais retouché depuis 0.2.4, donc la sonde annonçait 0.2.4 en servant
0.3.0, 0.3.1 puis 0.4.0. Trouvé en vérifiant le déploiement de 0.4.0 (le pod, lui,
tournait bien la bonne version : 9 outils google, mcp 1.29.0). Désormais dérivé des
métadonnées de la distribution → pyproject est la source unique, la dérive est
structurellement impossible. Un venv de dev installé une fois peut retarder ; l'image,
reconstruite à chaque release, est toujours exacte.

**v0.4.2 — le lien brouillon corrigé par l'e2e réel (2026-07-29)** : les deux
hypothèses de 0.4.0 étaient fausses, tranchées par l'essai en vrai, pas par le
raisonnement. (1) Le segment de compte doit être l'INDEX `/mail/u/0/` — la forme par
adresse `/mail/u/<email>/`, plausible et jamais vérifiée, rend un 404 ; désormais
surchargeable par `ROSETTA_GMAIL_ACCOUNT` si la boîte n'est pas le premier compte du
navigateur. (2) Le fragment doit être le **thread_id**, pas l'id de message : Gmail
remint ce dernier à chaque correction, donc tout lien déjà donné mourait. Le bug était
masqué sur un brouillon neuf, à qui Gmail donne un id de message ÉGAL au thread_id —
les deux constructions étaient indistinguables tant qu'on ne corrigeait pas. Du coup
`mail_draft_update` rend maintenant EXACTEMENT le même lien que le dépôt (bâti sur le
thread lu AVANT l'écriture, la réponse du PUT ne portant qu'un id transitoire qui
n'atterrit que sur le dossier #drafts). Appel `/profile` et son cache supprimés au
passage : plus rien à résoudre côté serveur. 34 tests.

**v0.5.0 — répondre dans un fil devient un seul paramètre (2026-07-29)** :
`mail_draft` prend un `reply_to_message_id` ; le serveur en dérive le `thread_id`, le
destinataire et l'objet (« Re: … », sans doubler un « Re: » déjà là). Le rattachement
au fil existait depuis toujours via `thread_id` — ce qui manquait, c'était le
**destinataire** : aucun outil n'exposait `Reply-To`, donc Alfred répondait au `From`,
qui est une adresse d'envoi automatique chez toutes les plateformes. Le brouillon
atterrissait dans le bon fil, bien chaîné, adressé à un `no-reply` — défaut invisible
jusqu'à l'envoi. `Reply-To` l'emporte désormais sur `From`, `References` porte la
chaîne du parent + le parent (le fil tient aussi chez le destinataire, pas seulement
dans notre Gmail), et l'outil rend les `to`/`subject` RÉELLEMENT utilisés pour qu'ils
soient vérifiables. `mail_thread` expose `reply_to` quand il existe, et l'`id` de
chaque message (c'est lui qu'on repasse en `reply_to_message_id`). Signature de
`mail_draft` : `body` d'abord, `to`/`subject` devenus optionnels. 37 tests.

**Prochaines étapes :**
- [ ] Alfred : le contournement posé dans la skill `correspondance` (réécrire le
      segment en /u/0/, ignorer le lien de l'update) devient inutile en 0.4.2 —
      et sa 2e moitié devient FAUSSE, le lien de l'update étant désormais le bon.
      À retirer côté cerveau (Alfred), pas ici
- [ ] Porter les addons sur `mcp` 2.x et lever le plafond `<2` (chantier à part :
      `mcp.server.fastmcp` a bougé, tous les addons passent par `_common`)
- [ ] Déployer 0.4.0 : tag v0.4.0 → image GHCR → bump tag k8s → rollout tantive,
      puis e2e réel (déposer un brouillon, le corriger, cliquer le lien) — exige un
      token humain, donc à faire par l'Alfred du pod
- [ ] ⚠️ **Hook `google_guard.py` d'Alfred PÉRIMÉ** (repo Alfred, cerveau) : il garde
      des noms workspace-mcp (`manage_event`, `draft_gmail_message`) qui n'existent
      plus. Conséquence RÉELLE : `calendar_update` ne matche plus `CAL_WRITE_TOOLS`
      → le bouclier 🛡 PWA est contourné en headless. À réécrire sur la surface
      rosetta (`mail_*` / `calendar_*`), avec les 2 nouveaux outils brouillon
- [x] Déployé 0.3.0 (2026-07-21) : tag v0.3.0 → image GHCR multi-arch → bump tag
      k8s (0.2.4→0.3.0, chart inchangé) → rollout tantive. Vérifié DANS le pod :
      pypdf 6.14.2, 7 outils dont mail_attachment, /health 200. Reste l'e2e Gmail
      réel (PDF) — exige un token humain, à faire par l'Alfred du pod sur demande
- [x] Phase 2 infra : volume /mnt/data/rosetta-mcp/data (chown 1000), client_secret.json
      provisionné (copie pod alfred → tantive), ingress enroll/callback derrière
      sso-authelia, pod 0.2.1 Running — vérifié live : 3 addons ok, 302 SSO sur
      /google/enroll, 403 token machine sur /google/, 200 maps intact
- [x] Phase 2 CLOSE (2026-07-20) : client OAuth WEB « rosetta » créé (l'ancien
      alfred-pod est type installed → pas d'URI custom), client_secret.json web sur le
      volume, enrôlement Sébastien vérifié — sub UTF-8 propre (fix 0.2.3 : Remote-User
      latin-1→UTF-8 + NFC), scopes exacts (gmail.readonly, gmail.compose,
      calendar.events), refresh token 600 côté serveur. 0.2.3 déployée (+ pages
      d'enrôlement stylées, fix threadId brouillon hors fetch best-effort)
- [x] Purge S__bastien.json faite (2026-07-20, geste nommé)
- [x] Phase 3 DÉPLOYÉE (2026-07-20) : Authelia client alfred (offline_access +
      audience rosetta + RS256 + consent implicit), agent-gw 0.21.0 (refresh tokens
      par utilisateur côté serveur, ROSETTA_USER_TOKEN par tour), bridge v2 (cascade
      session→machine), .mcp.json google → hub (0.2.4 : clé preferred_username).
      E2E : re-login PWA puis test Gmail ; si sub opaque → claims_policy Authelia
- [ ] Après e2e : dégraissage google des 2 images alfred (workspace-mcp + creds
      ~/.google_workspace_mcp) + avenant D17/D24 et skill correspondance (cerveau) —
      surface RÉELLE = 7 outils rosetta (dont `mail_attachment`), corriger la fiche
      périmée qui décrit workspace-mcp et promet une lecture de PJ qui n'existait pas
- [ ] Dégraissage agent-gw (0.21.0) : retirer `mcp_servers/` + les 3 clés d'API de
      l'externalSecrets d'alfred-helm.yml, après quelques jours sans accroc
- [ ] Client `agent-skippy` + bridge sur le Mac (sessions Skippy locales)
