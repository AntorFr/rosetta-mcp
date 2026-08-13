# Status — rosetta-mcp

> MàJ : 2026-08-13

**`mail` — 0.17.0 : la capacité suit la session (2026-08-13)**. Plus AUCUN mot de
passe de boîte dans le pod : l'addon échange le bearer de l'appelant contre un
token coffre (OpenBao auth JWT fédérée Authelia, rôle `rosetta-mail`) dont la
policy TEMPLÉE n'ouvre que `creds/<mail_local>` — le coffre arbitre, pas le code.
`mail_local` = claim Authelia calculé (CEL `emails[0].split("@")[0]`, attribut +
claims policies déployés le 13/08 ; ⚠️ la variable CEL est `emails` au PLURIEL et
validate-config ne compile PAS le CEL — banc docker local obligatoire). auth.py
expose désormais le token brut (`current_token`) à côté des claims. Cache mdp 10
min en mémoire. Reste : gestes coffre admin (mount jwt-authelia + rôle + policy
templée — script remis à l'utilisateur), puis E2E via Alfred.

**`mail` + `postier` — 0.16.0 : le courrier de la maison entre au hub (2026-08-13)**.
Deux addons nés de la migration mailbox.org → OVH Zimbra de la veille.

- **`mail`** (`identity = "user"`) : les boîtes familiales en IMAP pur — recherche,
  lecture, **brouillons seulement** (le jumeau souverain de `google` : même garde par
  la surface d'outils). La boîte est **dérivée de l'identité du token** :
  `preferred_username` → pliage accents/casse (le même que creds-sync) → 
  `<local>@MAIL_DOMAIN`, mot de passe dans `MAIL_PASSWORD_<LOCAL>` (external-secrets).
  Un brouillon-réponse chaîne `In-Reply-To`/`References` et honore `Reply-To` avant
  `From`. Plus les **alias jetables** via l'API OVH v2, bornés par construction à la
  boîte de l'appelant (liste/création/suppression — jamais ceux d'autrui, testé).
- **`postier`** (classe machine — construit pour Nestor, que `/mail` refuse par
  identité) : l'UNIQUE capacité d'envoi. Expéditeur **gelé** à `POSTIER_FROM`,
  destinataires filtrés par `POSTIER_ALLOWED` (défaut : le domaine familial), quota
  horaire en fenêtre glissante, journal + copie Sent par IMAP (échec de copie =
  information, jamais un mensonge sur l'envoi).
- 17 tests neufs (identité accentuée, fil de réponse, alias d'autrui refusé,
  liste blanche, quota glissant), suite complète à 247. Aucun réseau dans les tests
  (IMAP/SMTP factices, OVH par MockTransport).
- **Reste à faire (déploiement)** : bloc `externalSecrets` dans le HelmChart
  (MAIL_PASSWORD_* ← `creds/*`, OVH_* ← `apps/ovh`, POSTIER_PASSWORD ←
  `apps/nestor·zimbra_password`), MAIL_IMAP_HOST/MAIL_DOMAIN/POSTIER_FROM en env,
  et vérifier `ROSETTA_ADDONS` si la liste est explicite en prod.

**`git` — 0.15.0 : le proxy publie enfin pour de bon (2026-08-10)**. La 0.14.1 avait réparé
l'enveloppe d'auth ; il restait **deux pannes**, toutes deux trouvées en poussant pour de vrai
depuis le pod de Skippy, aucune visible depuis la suite de tests.

**1. La garde anti-force-push interrogeait le mauvais oracle, et refusait tout.**
`_check_commands` appelait `/compare/<old>...<new>` **avant** de relayer le pack — or `<new>`
est précisément le commit qu'on pousse : GitHub répond `404`, lu comme « ascendance
invérifiable », donc refus. **Toute mise à jour de branche existante était rejetée** ; seules
les créations passaient. Les 16 tests ne l'ont pas vu : leur mock répondait `ahead` à n'importe
quel sha, existant ou non — il validait une logique impossible en vrai.

> **Le correctif inverse l'ordre et change d'arbitre.** Une mise à jour de branche est
> streamée vers une ref jetable (`refs/heads/rosetta-scratch/<hex>`, créée depuis zéro : rien
> n'y est écrasable), ce qui **fait exister les objets chez GitHub**. La vraie ref est ensuite
> déplacée par `PATCH /git/refs/…` avec **`force: false`**, que GitHub refuse nativement si ce
> n'est pas un fast-forward. La garantie vient désormais du serveur qui détient la branche, et
> non d'une question posée trop tôt. Ref jetable supprimée dans un `finally`. Seul le préfixe
> de commandes est réécrit : le pack continue de streamer sans être bufferisé.

**2. Le hub n'émettait qu'un challenge `Bearer`.** Git ne sait pas le lire : il abandonne sur
« Authentication failed » **sans jamais appeler son credential helper**. Un pod porteur d'un
jeton valide ne pouvait donc pas pousser — et surtout, **le lot 2 était irréalisable** : le
helper censé porter le canal et le bouclier n'aurait jamais été appelé, quel que soit son code.
Un 401 sous `/git` répond maintenant `Basic realm="rosetta"` ; ailleurs, le pointeur RFC 9728
ne bouge pas et aucun navigateur n'est invité à demander un mot de passe sur un endpoint MCP.

Au passage : un refus revient désormais en **ligne `ng`** du report-status et non en HTTP 403.
Une fois le pack en vol, git encapsule l'échange en side-band et le corps d'erreur n'atteint
jamais l'utilisateur — qui ne voit que `RPC failed; HTTP 403`, sans motif. C'est ce qui a rendu
le diagnostic pénible. Limite assumée : **un push ne met à jour qu'une ref à la fois**.

**230 tests au vert.** Restent à écrire, faute de pouvoir les jouer ici : l'e2e réel contre
GitHub après déploiement, et le nettoyage d'une ref `rosetta-scratch/` qu'un crash aurait
laissée (aujourd'hui inoffensive, mais visible dans la liste des branches).

<details><summary>Historique — 0.14.0 déployée et morte à l'arrivée, 0.14.1</summary>

**0.14.0 : déployée, et morte à l'arrivée.** Le premier e2e réel — celui que la case plus bas
réclamait — a été fait depuis le pod de Skippy et le proxy a répondu **401 `invalid
credentials`** sur `info/refs`, hub parfaitement configuré.

**Un token, deux portes, deux enveloppes.** `_github_headers()` posait `Authorization: Bearer`,
ce qui est juste pour `api.github.com` et **faux** pour les endpoints smart-HTTP de
`github.com`, qui n'acceptent **que** Basic `x-access-token:<token>`. Mesuré des deux côtés le
même jour avec un PAT : Bearer → 401 `invalid credentials`, Basic → 200. C'est la symétrie
exacte de ce que `auth.py` venait d'admettre à l'autre bout — git ne sait pas *envoyer* un
Bearer, github.com ne sait pas en *recevoir* un ici.

**Ce qui a rendu la panne illisible** : le proxy streame le statut ET le corps amont tels quels.
Le refus de GitHub arrive donc dans le terminal du pousseur avec l'apparence d'un refus du hub —
on soupçonne son propre jeton rosetta, qui est valide. Les 16 tests passaient : aucun ne
regardait l'enveloppe envoyée en amont, seulement les refus. Un 17e la vérifie maintenant, et il
échoue sur la 0.14.0.

</details>

Le pod de Skippy
savait écrire du code et pas le publier. `repo_commit` passe les **contenus en ligne** dans
l'appel d'outil : publier 0.57.2 (186 Ko, dont un `main.py` de 72 Ko) lui demandait de retaper
chaque octet de mémoire — canal lossy, et un caractère de travers réécrit la source en silence.
Il a refusé, à raison ; c'est un humain qui a fini par extraire ses commits en `git bundle`.

**Le correctif est de déplacer l'objet, pas le contenu.** Le pod pousse du vrai git vers le hub,
qui relaie vers GitHub avec le jeton de l'App. **Le credential GitHub ne quitte jamais le hub** —
l'invariant que `repo_commit` protégeait est intact, son coût a disparu. Seul addon dont la
surface est du **HTTP nu** et non du MCP : `info/refs`, `git-receive-pack`, `git-upload-pack`.

⚠️ **Le piège qui aurait rendu la garde décorative : le protocole ne porte AUCUN drapeau de
force.** C'est le serveur qui tranche par ascendance, et **GitHub accepte un force-push sur une
branche non protégée**. Relayer tel quel n'aurait donc rien protégé — d'où la promotion par ref
jetable + `force: false` décrite en tête (la 0.14.x s'y prenait autrement, et refusait tout).
Refus également : suppression de ref, ref hors `refs/heads/*`+`refs/tags/*`, déplacement d'un
tag existant, push touchant plusieurs refs à la fois, et corps compressé sur receive-pack (il
aveuglerait l'inspection).

Les commandes sont lues en **pkt-line** en tête de corps, et **seul ce préfixe est bufferisé** —
le pack lui-même est streamé sans jamais tenir en mémoire.

**Décider s'il FAUT pousser n'est délibérément pas ici** : seul l'appelant sait si un humain est
devant. Ce jugement appartient au credential helper côté pod (canal + bouclier), qui reste à
écrire — c'est le lot 2. Trois couches, chacune gardant ce qu'elle voit.

16 tests neufs, **218 au total au vert**. Collision `/git` vs `/github` vérifiée absente
(`/github/enroll` atteint bien son addon).

**Reste à faire :**
- [ ] **Lot 2 — `git-credential-rosetta` côté agent-pods** : lit `GW_CHANNEL`, consomme le
      bouclier en PWA (même endpoint que `google_guard.py`), refuse sec en `planif`, puis
      seulement délivre le jeton. Le pod ne détient rien : contourner le helper, c'est se
      retrouver sans credential
- [x] **Lot 3 — déployer** : fait, `v0.14.0` taggée et en prod (`/` → `git: ok` en 0.14.0)
- [x] **Redéployer en 0.14.1** : fait, hub en 0.14.1 — mais il ne publiait toujours que des
      branches neuves, d'où la 0.15.0
- [ ] 🛡 **Tag `v0.15.0` + image + déploiement** : c'est ce qui rend le proxy réellement
      utilisable. Geste de Monsieur
- [ ] **Lot 2 débloqué, à écrire** : `git-credential-rosetta` est redevenu possible maintenant
      que `/git` émet un challenge Basic. Tant qu'il n'existe pas, on pousse par
      `http.extraHeader` — et **un push n'a alors aucune garde de canal**
- [x] e2e réel : fait depuis le pod de Skippy, **et c'est lui qui a trouvé le bug**. La leçon
      tient en une ligne : 218 tests au vert ne prouvaient pas qu'un seul push aboutissait
- [ ] Refaire l'e2e après le redéploiement, avant de basculer les remotes des pods
- [ ] Une fois éprouvé : côté Alfred, D46 se rediscute (elle est adossée à ce manque)

**`marees` — les horaires et le coefficient, 0.13.0 DÉPLOYÉE (vérifié en prod le 2026-08-10 :
`/health` → 9 addons `ok` en 0.13.0)** :
la skill `balades` d'Alfred disait « marée si le parcours touche l'estran — tu n'as pas
d'outil, dis-le plutôt que de deviner ». Voilà l'outil. Périmètre **volontairement étroit**,
fixé par Monsieur : les **heures** et le **coefficient**, rien d'autre — pas de courbe de
hauteurs, pas de marnage, pas de seuils, pas de planification nautique. Deux questions,
deux seulement : « l'estran est-il découvert à 15 h ? » et « ça va tirer fort samedi ? ».

⚠️ **LE COEFFICIENT N'EST PAS LOCAL — c'est la première chose vérifiée, et elle corrige la
question posée.** Il est calculé pour le port de **Brest** et vaut identiquement sur la Manche
et l'Atlantique (l'onde y arrive peu déformée). Ce qui varie avec le lieu, ce sont les
**heures**. Le même 100 donne ~6 m de marnage à Brest, **plus de 13 m** au Mont-Saint-Michel et
**0,5 m** en Méditerranée, où la notion n'a aucun sens. D'où la forme des réponses : un
coefficient par marée, des heures par port.

**Source : api-maree.fr** (niveaux calculés sur les composantes harmoniques Ifremer/PREVIMER).
Gratuite, une clé de compte, 360 req/h, fenêtre **J−30 → J+30**. ⚠️ Son coefficient est déclaré
**NON OFFICIEL par la source elle-même** : le Shom fait autorité et **vend** son service
SPM/SAPM. Bon pour décider une sortie, pas pour un document qui engage — et chaque réponse le
dit.

**Le Shom a été écarté délibérément, pas par ignorance.** Son portail gratuit appelle bien un
service interne (le bundle parle `coeff`, `hlt`, `wl`), mais s'en servir reviendrait à utiliser
sans payer une API qu'ils commercialisent. On n'a pas cherché la clé.

Deux gardes **dans le code**, pas seulement dans la doc : la **distance au port** est toujours
rendue et signalée au-delà de 25 km (un port à 50 km ne prédit pas votre estran), et une date
**hors J±30 est refusée** au lieu d'être extrapolée — déborder en silence inventerait une marée.
Un lieu est résolu d'abord sur les 131 ports connus, puis par le **géocodeur IGN** (sans clé) :
pas de troisième clé pour une question que la donnée ouverte règle déjà.

**VÉRIFIÉ SUR LE SERVICE VIVANT le 2026-08-06**, avec une clé de test que Monsieur a fournie
puis fait tourner. Trois enseignements, dont deux corrigent le code :

1. ⚠️ **La forme supposée était fausse.** `data` est une liste de **JOURS** portant chacun ses
   `extrema` ; les heures sont déjà en « HH:MM » locales, et le coefficient s'appelle **`coef`**.
   Le parseur tolérant a fait exactement son office (il aurait remonté la charge brute au lieu
   d'un objet vide) et les fixtures sont désormais la **vraie** réponse, pas une devinette.
2. ⚠️ **« Sète » rendait les marées de BORDEAUX**, à 389 km et sur une autre mer, sous un
   avertissement « ordre de grandeur » — ce qui est faux : ce n'est pas un ordre de grandeur,
   c'est autre chose. Les 131 ports couvrent Manche, Atlantique et mer du Nord, **aucun en
   Méditerranée**. Au-delà de 100 km, c'est désormais un **refus** qui nomme la raison ; entre
   25 et 100 km, un avertissement.
3. **La donnée est en CC BY** : l'attribution est la condition de la licence, pas une politesse.
   Celle de la source est reprise telle quelle (débarrassée de son HTML) dans chaque réponse.

**Et la donnée confirme la documentation.** Le 7 août 2026, coefficient **47 à Brest, 47 à
Auray, 47 à Saint-Malo** — identique, comme annoncé. Les hauteurs, elles, vont de 3,85 m à
9,79 m. C'est exactement l'écart que la note « le coefficient n'est pas local » décrit.

**Reste à faire :**
- [ ] **La clé au coffre** puis dans le manifeste (`API_MAREE_KEY`). Compte créé, clé de test
      validée puis tournée par Monsieur. Sans clé l'addon monte **degraded** et le dit.
- [ ] Publier 0.13.0 — **bloqué : GitHub Actions en panne majeure** depuis le 2026-08-06 15h22.
- [ ] Côté cerveau : retirer de la skill `balades` la phrase « tu n'as pas d'outil de marée ».

> MàJ précédente : 2026-08-04

**`trace` — les balades sur OpenStreetMap, 0.12.0 ÉCRITE, PAS ENCORE DÉPLOYÉE
(2026-08-04)** : Alfred fabriquait ses GPX à la main — il tapait les 328 `<trkpt>` de la
boucle de Vannes au clavier, cinq commits en 24 h pour la même trace, et **zéro `<ele>`**
dans le fichier alors que le routeur les lui rendait. Deux outils, **aucune clé** :
`trace_calcule` (BRouter/OSM) rend distance, D+/D−, revêtement, mètres d'escaliers,
distance de chaque étape et **écart de chaque repère à la trace** ; `trace_pois` (Overpass)
rend ce qu'un annuaire de commerces ne connaît pas — eau potable, points de vue, abris,
bancs, sentiers balisés. Le tourisme reste chez `search_places` (addon `maps`) : les notes
et le **nombre d'avis** sont à Google, OSM n'a pas d'équivalent.

⚠️ **La géométrie ne traverse JAMAIS le modèle.** `trace_calcule` rend les chiffres et une
URL ; `GET /trace/geometrie` rend la trace encodée en polyline, que l'appelant écrit
directement sur disque. Mesuré sur Vannes : **2 300 octets** contre 13 000 de GPX, et
~700 tokens vus par l'agent contre ~12 000 **deux fois** (lecture + réécriture).
La route est **sans état** — l'URL porte les mêmes paramètres, donc on recalcule au lieu de
tenir un cache qui demanderait une durée de vie, une taille et un nombre de réplicas.

**Validation e2e contre le travail à la main d'Alfred** (boucle de Vannes, 19 repères) :
3 036 m et 328 points **au mètre près**, étapes 410/266 (Garenne) et 315 (Connétable →
jardins) identiques à sa fiche, et l'écart de **27 m** du bastion de Gréguennic — qu'il
avait mesuré à la main — retrouvé tout seul. En prime, deux faits qu'il n'avait pas :
**67 m d'escaliers** et 1 298 m de pavés.

Quatre comportements amont, tous **mesurés sur les services vivants** le 2026-08-04, jamais
lus dans une doc : (1) BRouter parle **`lon,lat`**, l'inverse de tout le reste du hub — se
tromper rend un itinéraire plausible dans le mauvais hémisphère, pas une erreur, d'où le
retournement dans **une seule** fonction ; (2) ses échecs ne sont **pas du JSON** — 400 +
texte brut pour un îlot injoignable, **500 + corps VIDE** pour un profil inconnu ; les noms
de profil sont sensibles à la casse, le seul profil piéton est `hiking-beta`, et `trekking`
est un profil **vélo** malgré son nom ; (3) le dénivelé est filtré par **hystérésis 5 m**,
une seule méthode pour les deux sens — l'accumulation brute gonflait une boucle réelle de
9,5 km de 378 à 506 m ; (4) Overpass tronque sur un plafond **commun**, donc « eau, vue,
banc » revenait en bancs seulement — un jeu nommé et un plafond **par type**.

Le piège qui a mordu à l'e2e : un repère s'ancre au sommet le plus proche **en cherchant
vers l'avant seulement**. Sur une boucle, le dernier point EST le premier, et une recherche
globale le ramenait à l'indice 0 — les 260 m de retour au port devenaient 2 770 m comptés à
l'envers autour de la ville.

`altimetrie="ign"` reprofile sur **RGE ALTI** (IGN Géoplateforme, 1 m, France, gratuit et
sans clé). Sur la boucle de Chartreuse les deux modèles s'accordent à 1 % : c'est une
**option**, pas le défaut — elle ne paie son aller-retour que sur terrain fin. Deux pièges
IGN : le service **rééchantillonne le long de la ligne** au lieu de répondre aux sommets
qu'on lui donne, et les booléens doivent partir en **chaînes** `"true"`/`"false"`.

**Prochaines étapes :**
- [ ] Déployer 0.12.0 et vérifier `trace` **par le pont** (e2e), pas seulement sur `/health`
- [ ] Côté pod (agent-pods) : le rapatriement de `/trace/geometrie` dans le fichier de
      parcours, puis l'assemblage du GPX au téléchargement
- [ ] Auto-héberger BRouter si l'usage tient (l'instance publique est une courtoisie, sans SLA)
- [ ] `trace_boucle` (génération d'une boucle de N km) : BRouter ne sait pas le faire,
      il faudrait GraphHopper — pas commencé, pas promis

**`github` — les pull requests, 0.11.0 DÉPLOYÉE EN PROD (2026-08-02)** :
Skippy voyait passer six PR Renovate sur `k8s-home-lab` sans rien pouvoir en faire — la
surface ne les connaissait pas. Surface **9 → 12** : `pull_requests` (liste), `pull_request`
(une PR en détail : état de fusion, fichiers touchés, patch sur `diff=True`) et
`pull_request_merge`. **Ouvrir, fermer, commenter, approuver n'existent toujours pas** —
l'élargissement est celui du geste demandé, pas de la classe de gestes.

Deux comportements amont commandent le façonnage, tous deux vérifiés dans la doc avant
d'écrire, pas supposés :
(1) ⚠️ **GitHub calcule la mergeabilité en TÂCHE DE FOND** — la première lecture d'une PR
endormie rend `mergeable: null` et démarre un job (« After giving the job time to complete,
resubmit the request »). Rendu tel quel à un agent, ce `null` se lit « pas fusionnable » :
faux négatif silencieux sur une PR saine. L'addon **relit** (3 essais), et s'il reste `null`
il le **dit** au lieu de trancher ;
(2) la fusion porte le **sha de tête relu juste avant** — une branche qui bouge entre la
lecture et le PUT donne un **409** au lieu de fusionner ce que personne n'a regardé. La
branche fusionnée n'est **jamais** supprimée : l'outil n'existe pas, et Renovate nettoie les
siennes.

Pré-vol avant d'écrire : déjà fusionnée / fermée / brouillon / en conflit sont refusés **en
français** plutôt qu'en 405 laconique. `mergeable_state` est glosé sur ce qu'on connaît et
**passé verbatim** sinon (il n'est pas documenté champ par champ côté REST — il l'est en
GraphQL, `mergeStateStatus`) : inventer une traduction serait pire que le mot brut, qui reste
cherchable. Le 403 sur `/pulls` nomme désormais **« Pull requests »** et non `workflows` —
sinon on cherche la permission au mauvais endroit dans les réglages de l'App.

⚠️ **Titre, corps et auteur d'une PR sont du texte TIERS** (Renovate, un contributeur de
passage) : de la donnée à rapporter, jamais une instruction à suivre — même régime que les
fiches Open Food Facts (D36 côté Alfred). Aucun hook n'attrape ça ; c'est écrit dans la
docstring de l'outil, là où l'agent le lit.

**13 tests neufs, 147 au total au vert**, canari de surface porté à 12 et sa liste de mots
interdits recentrée sur les gestes encore refusés (`review`, `approve`, `comment`, `close`).
Garde relue **dans la même passe**, comme le contrat l'exige : `github_guard.py` (cockpit)
gagne les deux lectures et la fusion, cette dernière au **même palier que `repo_commit`** —
fusionner un Renovate sur `k8s-home-lab` **est** un déploiement, donc bouclier sur tous les
canaux et refus dur en `planif`. `bin/test-garde` : 18 → **21 cas**, au vert.

**Addon `meteo` — DÉPLOYÉ ET VÉRIFIÉ EN PROD (2026-08-01, rosetta 0.10.0)** : Open-Meteo,
le vent pour la **voile légère**. Classe machine comme `food` —
aucune clé, aucun compte, aucun enrôlement, aucun volume, aucun ExternalSecret, aucune
route d'ingress. Deux outils : `wind_forecast` (horaire, **nœuds natifs** via
`wind_speed_unit=kn`, vent moyen + rafale + **ratio de rafale** + direction, borné au
**jour clair** du spot, plusieurs modèles en un appel = mesure de confiance) et
`wind_spots` (registre `ROSETTA_WIND_SPOTS`).

**Pourquoi pas `maps`, qui parle déjà météo** : Google, c'est MetNet — un modèle fermé,
aucun choix. Ce qui tranche une sortie, c'est la rafale à côté du moyen et l'**accord
entre modèles** ; AROME France HD résout 1,5 km là où un modèle global voit 25 km, et une
brise côtière tient tout entière dans cet écart.

**LA décision de conception : un appel HTTP PAR MODÈLE, jamais groupé.** Ce n'est pas du
zèle, c'est ce qui rend deux pièges *impossibles* au lieu de les rustiner :
(1) un modèle **hors domaine disparaît** d'une réponse groupée — pas d'erreur, pas de
colonne `null`, HTTP 200 (AROME HD + ECMWF sur Québec rend les chiffres d'ECMWF seuls,
octet pour octet identiques à ECMWF demandé seul) ; demandé **seul**, il est honnête :
`400 / "No data is available for this location"` ; (2) le **suffixe de clé dépend du
nombre de SURVIVANTS**, pas de la demande — 2 demandés, 1 revenu, et la clé est le
`wind_speed_10m` nu, qu'un parseur naïf attribue au mauvais modèle. Un modèle par appel →
clé toujours nue, absence toujours criée. Coût : N appels sur un budget de 600/min.

Les autres pièges, tous mesurés : (3) **hors horizon c'est l'inverse** — des `null`, pas
une disparition (AROME HD mesuré à **69 h** quand la doc annonce « 2 days ») ; les lignes
nulles sont **écartées, jamais lues comme un calme plat** ; (4) le **géocodage noie** — «
La Torche » résout dans **l'Allier, 367 m d'altitude**, et rend 5,5 kn quand la vraie
pointe en rend 5,2 : deux chiffres également crédibles, d'où le registre en premier et un
**avertissement + région + altitude** sur tout lieu deviné.

⚠️ **Bug trouvé en faisant tourner l'addon pour de vrai, pas en le relisant** :
`timezone` était figé sur le fuseau de la maison, donc à Québec le coucher du soleil tombe
le **lendemain** en heure de Paris → fenêtre inversée → réponse « créneau vide ». Corrigé
en `timezone=auto` (l'heure locale **du spot**, ce que la doc de l'outil promettait déjà),
avec repli sur la journée pleine si la fenêtre ressort à l'envers. Idem la moyenne des
directions : **circulaire** (atan2), parce que 350° et 10° donnent le **nord**, pas le sud
— sur un spot à shore break c'est la différence entre onshore et offshore. Et un test a
attrapé un cap à **360°** (modulo appliqué avant l'arrondi).

Pas de `_Quota` ici, délibérément : Open-Meteo alloue **600/min et 10 000/jour** contre
15/min chez OFF — quarante fois plus large, et une sortie se planifie en quelques appels.
Licence **CC-BY 4.0** → chaque réponse nomme sa source.

**28 tests neufs, 134 au total au vert**, addon découvert et monté (`/health` → `meteo:
ok`, 7 addons). **Vérifié en réel de bout en bout par le code de l'addon** (pas curl) :
La Torche 3 modèles → AROME HD 6,8 kn / ARPEGE 5,2 / ECMWF 3,8, **écart 3,0 kn**, rafales
et ratios cohérents ; Québec → AROME HD crie son 400, ECMWF répond ; J+6 → AROME HD dit
son horizon ; modèle bidon → erreur amont rapportée telle quelle.

**Déploiement (2026-08-01)** : v0.10.0 → CI verte (tests + build) → image GHCR multi-arch
**vérifiée présente avant le bump** (amd64 + arm64) → tag 0.8.0→0.10.0 et
`ROSETTA_WIND_SPOTS` posés dans `clusters/tantive/home/mcp/rosetta-mcp-helm.yml` → refresh
ArgoCD forcé. **Rien d'autre touché**, comme prévu : pas de secret, pas d'ExternalSecret,
pas d'ingress. Vérifié en prod : `/health` → **7 addons `ok` en 0.10.0**, `/meteo/` sans
jeton → **401**, métadonnées RFC 9728 → 200. Et **l'e2e par le vrai trajet** — pod Alfred →
`rosetta-bridge meteo` → client_credentials Authelia → hub → Open-Meteo : `wind_spots` rend
le registre de prod, `wind_forecast` sur le club rend AROME HD 4,6 / ARPEGE 5,3 / ECMWF 3,9
kn, **écart 1,4 kn**, en heure locale et en nœuds.

> 🔎 **Gotcha — `kubectl exec -i` derrière `ssh` ne fait pas passer stdin, et le pont
> meurt sur EOF.** Deux échecs muets superposés (sortie vide, aucune erreur) : la double
> couche ssh→kubectl n'a pas relayé le tuyau, puis `rosetta-bridge` s'est arrêté dès la fin
> de son entrée, avant le retour HTTP. La forme qui marche pour tout e2e MCP en pod :
> encoder le JSON-RPC en base64, le dérouler **dans** le conteneur, et **tenir stdin
> ouvert** — `sh -c '{ echo <b64> | base64 -d; sleep 25; } | rosetta-bridge <addon>'`.

**⚠️ `maps` a enfin rencontré sa vraie API (2026-08-01)** — le seul façonnage du hub qui ne
l'avait jamais fait. Vérifié depuis le pod (la clé y vit) : `weather_now` rend bien la
rafale (16 km/h moyen, 23 en rafale, N), `weather_forecast` rend **le vent qu'il jetait**
(11 / 23 km/h, NNE), et `weather_hourly` sort la ligne **`2026-08-02T00:00`** — le pari
proto3 sur la clé `hours` absente à minuit tient, mesuré et non plus supposé.

**Le registre compte moins que prévu (arbitrage du 2026-08-01)** : les spots de Sébastien
**dépendent du lieu de vacances**, donc inconnus à l'avance — seul le club de voile de
**Carquefou** est durable. Le chemin qui porte le poids n'est donc pas le registre mais le
**texte libre**, et le géocodeur Open-Meteo ne connaît que des **communes** : « Carquefou »
rend le bourg à 34 m, jamais l'Erdre. Deux conséquences encodées : les **autres candidats**
remontent au lieu d'être jetés (« Saint-Pierre » en rend trois), et l'avertissement pointe
vers la **composition avec `search_places`** (addon `maps`), qui connaît les lieux et pas
seulement les villes — chercher le club là-bas, repasser ses « lat,lng » ici. Les spots de
vacances n'ont donc rien à faire en variable d'environnement : ils vivent dans la mémoire
d'Alfred, au voyage, où ils s'écrivent sans rollout.

**`maps` — le vent enfin lu (2026-08-01, rosetta 0.9.0, DÉPLOYÉ dans la 0.10.0)** :
`weather_forecast` **jetait le bloc `wind`** que la Weather API renvoie sous
`daytimeForecast`, dans la réponse déjà facturée — donc « ça souffle demain ? » était
structurellement sans réponse, sans que rien ne le signale. Corrigé, et étendu :
`weather_now` gagne la **rafale** et les degrés, et un 6ᵉ outil `weather_hourly`
(heure par heure, température / ciel / pluie / vent) répond à « il pleut à quelle
heure ? ». Direction rendue en **cardinal français dérivé des degrés** (« NNO », « O »),
pas en traduisant l'enum Google (`NORTH_NORTHWEST`) : l'arithmétique survit à un enum
absent ou `UNSPECIFIED`, et il n'y a pas de table de 16 entrées à désynchroniser.

Trois choix encodés, tous vérifiés dans la doc plutôt que supposés :
(1) `weather_hourly` **plafonne à 24 h** — `pageSize` de `forecast/hours:lookup` est
« a value from 1 to 24 (inclusive) », donc au-delà il faut paginer par `nextPageToken`
pour une question qui ne dépasse jamais demain ; (2) ⚠️ **proto3 JSON omet les scalaires
nuls, donc minuit arrive SANS sa clé `hours`** — lue comme `None`, elle blanchirait une
ligne sur vingt-quatre ; le défaut à 0 est juste que le champ soit présent ou absent ;
(3) une rafale non prévue est **omise, jamais rendue à 0 km/h** (même règle que withings
et food : un trou n'est pas un calme plat).

Le façonnage a été écrit contre la doc REST (`ForecastHour`, `Wind`, `pageSize`), la clé
vivant côté cluster — et **confronté à la vraie API au déploiement** le jour même, depuis
le pod : les trois choix ci-dessus tiennent (cf. le bloc `maps` en tête de fiche).

**Et `maps` avait ZÉRO test** — le plus vieil addon du hub, le seul nu, ce qui est
exactement pourquoi le vent a pu être jeté des mois sans que personne le voie. Couture
`_transport`/`_client()` posée (patron de `food`), **15 tests neufs, 106 au total au
vert**.

**Addon `food` — DÉPLOYÉ (2026-07-31, rosetta 0.8.0)** : Open Food
Facts, 2 outils en **lecture seule** — `food_product` (un code-barres ou un panier
entier, 15 max par appel) et `food_search` (repli plein texte, moteur
`search.openfoodfacts.org`). Classe **machine** : lecture anonyme, donc **aucun secret,
aucun enrôlement, aucune route d'ingress, aucun ExternalSecret** — `ROSETTA_ADDONS`
n'étant pas positionné sur tantive, l'addon se monte seul au rollout. Écriture
délibérément absente (la base est communautaire, donc *éditable*) : c'est la garantie
qu'un agent ne publiera pas dans une base publique au nom de Monsieur, sans hook.
**Le MCP tout fait a été écarté après examen** (`domdomegg/openfoodfacts-mcp` : stdio
Node — le transport qu'on démonte — 3 écritures + un `call_api` brut, surface non
bornée) : l'API de lecture est un GET, l'addon fait 300 lignes.

Les cinq pièges, **tous mesurés en vrai le 2026-07-31**, quatre silencieux :
(1) produit absent = `status: 0` sous **HTTP 200 *ou* 404** selon le cas — le code HTTP
seul ne dit rien, et « absent » est une réponse (`trouve: false`), pas une panne ;
(2) `fields=` est **obligatoire** : 365 clés et 148 724 octets sans projection, 252 avec
(× 590) ; (3) OFF sert une **page HTML** en 503 quand il sature — `.json()` lèverait ;
(4) `ecoscore_grade` est la clé vivante, **pas** `environmental_score_grade` malgré le
renommage amont en Green-Score (vérifié sur un produit noté : Bjorg → `"a"`) ;
(5) ⚠️ **la projection ne sélectionne pas, elle re-rend** — `fields=allergens` renvoie
« milk, nuts, soybeans » là où le document complet porte « lait, fruits à coque, soja »
(`allergens_lc: fr`). Ni `lc=fr` ni le sous-domaine `fr.` n'y changent quoi que ce soit
(les trois mesurés) → on demande les `_tags` et on traduit ici, sur la liste fermée des
allergènes UE. Trouvé **uniquement** par l'appel réel : les fixtures ne pouvaient pas
l'inventer.

> ⚠️ **Le quota OFF se compte par IP, donc par déploiement — pas par appelant.** 15
> lectures produit et 10 recherches par minute, dépassement = bannissement de l'IP de
> sortie du cluster, partagée avec **tout le reste de la maison**. D'où le `_Quota`
> (fenêtre glissante, verrou tenu pendant l'attente pour une file FIFO) : c'est la
> raison d'être du module, pas un ornement. État par processus → **exact seulement en
> réplique unique** (déjà le cas, cf. withings/github).

20 tests neufs, **91 au total au vert**. Vérifié **en réel** de bout en bout via le code
de l'addon (pas curl) : Nutella → Nutri-Score E, NOVA 4, allergènes FR ; Bjorg muesli →
Nutri-Score A, Eco-Score A, traces FR ; recherche « yaourt brassé vanille » → 2 produits
Cora/Leader Price notés. **Déployé et vérifié en prod** : `/health` → `food: ok` en 0.8.0
(6 addons au vert), `/food/` sans token → 401, métadonnées RFC 9728 → 200, et surtout
**l'e2e par le vrai trajet** — pod Alfred → `rosetta-bridge food` → client_credentials
Authelia → hub → OFF, `food_product("3017620422003")` rendant Nutella / Nutri-Score E /
NOVA 4 / allergènes en français. `.mcp.json` et section `CLAUDE.md` posés côté cerveau
(D38), lecteur de code-barres livré dans la PWA (agent-gw 0.44.0).

**Addon `github` — DÉPLOYÉ (2026-07-31, rosetta 0.7.0)** : la surface d'écriture
du pod Skippy, sur le patron de `google`. Classe user-data (`identity = "user"`) : credential
côté serveur, un fichier par `sub` sous `ROSETTA_GITHUB_DATA`, enrôlement navigateur unique
(`/github/enroll`) gardé par le forwardAuth. **Le pod ne voit jamais le jeton** — c'est ce qui
fait que la garde n'est pas qu'un hook : un agent qui a un shell ne contourne pas un
credential qu'il n'a pas.
Surface : **7 lectures** (repo_list, repo_file, repo_tree, repo_commits, repo_search_code,
repo_tags, actions_runs) et **2 écritures** — `repo_commit` (créer/modifier/**supprimer** en
un commit atomique via l'API Git Data : blob → arbre → commit → ref ; `contenu: null`
supprime, donc la suppression n'est jamais une capacité à débloquer) et `repo_tag`. N'existent
pas, et c'est la garantie : création/suppression de dépôt, fork, suppression de branche,
force-push, issues, PR, secrets d'Actions, réglages, collaborateurs. Un test le **verrouille**
(compte d'outils + liste de mots interdits) : ajouter un outil hors contrat casse la suite.
⚠️ Deux pièges encodés : GitHub **fait tourner le refresh token** à chaque usage (on restocke,
sinon ré-enrôlement au tour suivant → 2ᵉ composant single-writer avec withings, réplique
unique obligatoire), et un 403 nomme explicitement `workflows: write`, permission distincte de
`contents: write` qu'exige tout commit touchant `.github/workflows/`.
12 tests neufs, **69 au total au vert**, addon découvert et monté (`identity=user`).
Vérifié en prod : `/health` → `github: ok`, `GITHUB_CLIENT_ID`/`SECRET` présents dans le
Secret (ExternalSecret synchronisé, Withings intact), `/github/enroll` → **302 vers Authelia**
et `/github/` (MCP) → **401**. **Reste : l'enrôlement navigateur de Monsieur**, puis le hook
`github_guard.py` côté cockpit skippy.

> 🔎 **Gotcha — un addon user-data qui gagne un enrôlement doit être ajouté à l'ingress
> `enroll`.** Les chemins y sont énumérés un par un ; oublié, `/github/enroll` retombe sur
> l'ingress principal, **sans forwardAuth**, donc sans en-tête `Remote-User` — et la page
> répond « accès refusé » sans que rien n'indique que l'ingress est le fautif. Attrapé avant
> mise en service le 2026-07-31, en relisant le chart plutôt qu'en testant à l'écran.

> 🔎 **CI : un échec au *boot de buildx* n'est pas un échec de build.** Le 2026-07-31, le tag
> v0.7.0 est mort sur `docker buildx create` → `moby/buildkit` intirable depuis Docker Hub
> (`context deadline exceeded`) : le Dockerfile n'a jamais été lu, les tests jamais lancés.
> Indice décisif : le build `main` du **même commit** était vert. `gh run rerun --failed`
> suffit — ne pas partir chercher un bug qui n'existe pas.

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

**v0.6.0 — addon `withings` DÉPLOYÉ sur tantive (2026-07-31)** : 2e addon de
classe user-data, **lecture seule** — 5 outils (`withings_measures`, `_activity`,
`_sleep`, `_workouts`, `_devices`). Mesures rendues avec label FR + unité (table des
38 types Withings, filtre par nom/alias/code : « poids », « tension », « composition
corporelle », « 1,6,8 »). Enrôlement navigateur `/withings/enroll` → consent →
`/withings/callback`, même patron que google (state HMAC, forwardAuth ingress), mais
credentials du client par **env** (`WITHINGS_CLIENT_ID/SECRET`) → l'addon dégrade
proprement sur `/health` s'il n'est pas provisionné. Page d'enrôlement factorisée dans
`_common.enrol_page()` (google refactorisé dessus, tests inchangés). 20 tests, 57 au
total. Nouvelle dep : `tzdata` (zoneinfo a besoin d'une base tz, non garantie sur une
image slim — je n'ai PAS vérifié ce que porte `python:3.12-slim`, j'ai supprimé la
question).

Les 3 pièges Withings, tous silencieux, tous traités et testés : (1) **tout répond
HTTP 200**, y compris les échecs — le vrai statut est `status` DANS le corps (0 = OK) ;
(2) le **refresh token tourne** à chaque rafraîchissement et tue le précédent → écriture
atomique avant usage, un verrou par utilisateur, token d'accès caché ses 3 h pleines, et
**une seule réplique** (deux se brûleraient mutuellement le jeton) ; (3) une mesure est
un couple `(value, unit)` où `unit` est une **puissance de dix** — 78192/-3 = 78,192 kg
(Decimal, sinon le float rend 78.19200000000001).

**Prochaines étapes :**
- [x] **Permission « Pull requests » (lecture) posée sur la GitHub App et approuvée au
      niveau du compte** par Monsieur (2026-08-02). La fusion, elle, passe par
      `contents: write` déjà déclaré — *tenu de la table des permissions de la doc GitHub,
      pas d'un essai* : si un 403 tombait sur le PUT, passer « Pull requests » en Read &
      write. Le message d'erreur de l'addon nomme la permission, l'erreur s'expliquera
      d'elle-même
- [x] **0.11.0 déployée et vérifiée en prod (2026-08-02)** : tag `v0.11.0` → CI verte
      (tests + build) → image GHCR **vérifiée multi-arch avant le bump** (amd64 + arm64
      présents au manifeste) → tag bumpé dans `clusters/tantive/home/mcp/rosetta-mcp-helm.yml`
      → ArgoCD `home` resynchronisé sur `fbd04fc`. **Rien d'autre touché**, comme prévu : pas
      de secret, pas d'ExternalSecret, pas d'ingress. Vérifié dans le pod : `/health` → **7
      addons `ok` en 0.11.0**, et l'image déployée expose bien **12 outils** github dont les
      trois PR
- [ ] **e2e réel — LE SEUL RESTE, et il ne peut pas venir d'ici.** Il exige un token humain,
      donc c'est le Skippy du **pod** qui le fera depuis la PWA : lister les PR de
      `k8s-home-lab` (six Renovate ouvertes au 2026-08-01), en lire une en détail, en
      fusionner une sous bouclier 🛡.
      ⚠️ **Ne PAS le simuler depuis le Mac par `kubectl exec`** en appelant
      `_access_token(sub)` : ça marcherait, et ça ferait TOURNER le refresh token dans un
      second processus — exactement le viol de single-writer que tout le reste du dépôt
      s'échine à empêcher. Le pod garderait un jeton mort et Monsieur devrait se ré-enrôler.
      La tentation est réelle ; la réponse est non
- [ ] Cockpit : `git pull` **dans le pod** (`kubectl -n home exec deploy/skippy`) — refusé au
      Mac par le classifieur de permissions le 2026-08-02, donc à faire par Monsieur ou
      depuis la PWA. Le hook vit dans le workspace, pas dans l'image : poussé ≠ actif, et le
      grep sur le fichier réel est la seule preuve
- [x] **Le club de Carquefou dans `ROSETTA_WIND_SPOTS`** (le seul spot durable), posé
      dans `clusters/tantive/home/mcp/rosetta-mcp-helm.yml` au déploiement de 0.10.0 :

      ```yaml
      ROSETTA_WIND_SPOTS: '{"SNO Carquefou":{"latlng":"47.30144,-1.52660","note":"Sport Nautique de l''Ouest — 17 chemin de Port Breton, sur l''Erdre"}}'
      ```

      Coordonnées **vérifiées**, pas devinées : club nommé par Sébastien
      (`snonantes.fr`) → adresse relevée sur le site → géocodée par la **Base Adresse
      Nationale** (`api-adresse.data.gouv.fr`, autorité sur les adresses FR, sans clé),
      qui rend un `housenumber` à 47.30144,-1.52660. Contrôle de vraisemblance :
      Open-Meteo donne **8 m d'altitude** à ce point contre **34 m** au bourg de
      Carquefou — c'est bien l'Erdre. ⚠️ Le géocodeur d'Open-Meteo, lui, aurait rendu le
      bourg : il ne connaît que des communes
- [x] **0.10.0 déployée et vérifiée en prod (2026-08-01)**, e2e par le pont compris, et
      le façonnage `maps` confronté à la vraie Weather API au passage (cf. plus haut)
- [x] Alfred (cerveau) : `meteo` câblé (`.mcp.json` + section `CLAUDE.md`, commit
      `conf: meteo …`), avec la lecture qui compte — la rafale et le ratio priment sur le
      vent moyen, l'écart entre modèles EST la confiance, un lieu `resolution: geocodage`
      se vérifie avant d'être cru, et les spots de séjour s'écrivent dans le dossier du
      voyage. Deux faits périmés de la section `maps` corrigés au passage (« serveur
      local » alors qu'elle passe par `rosetta-bridge`, et `weather_hourly` absent)
- [ ] Une **décision D42** consignant `meteo` dans `DECISIONS.md` d'Alfred reste à écrire
      si Monsieur veut le « pourquoi » dans le journal des décisions — la section
      `CLAUDE.md` est pour l'instant autoportante, sans référence pendante
- [ ] Alfred (cerveau) : la section « Maps & météo » de `CLAUDE.md` ignore
      `weather_hourly` et décrit `maps` comme un « serveur local » alors qu'il passe par
      `rosetta-bridge` depuis la 0.1.0. À corriger côté Alfred, pas ici
- [x] **`food` déployé (2026-07-31)** : v0.8.0 → image GHCR multi-arch → tag bumpé dans
      `clusters/tantive/home/mcp/rosetta-mcp-helm.yml` → rollout ArgoCD. **Rien d'autre
      n'a été touché**, comme prévu : pas de secret, pas d'ExternalSecret, et pas
      d'ingress non plus — l'ingress principal est un catch-all `/` (relu, pas supposé)
      et l'addon n'a aucune route d'enrôlement. Vérifié en prod, e2e compris
- [ ] **Withings — l'enrôlement et l'e2e restent à faire.** Ouvrir
      `https://rosetta.mcp.berard.me/withings/enroll` au navigateur (SSO → consentement
      Withings), puis demander ses mesures à Alfred depuis la PWA : ça exige un **token
      humain**, donc c'est l'Alfred du pod qui déclenche le premier appel réel.
      ⚠️ **Aucun appel réel à l'API Withings n'a encore eu lieu** : la forme des réponses
      vient de la doc et d'`aiowithings`, pas d'un essai. C'est là que ça se vérifiera
- [x] Withings déployé (2026-07-31) : app OAuth enregistrée par Sébastien (callback
      `https://rosetta.mcp.berard.me/withings/callback`), `secret/apps/rosetta-mcp`
      (`withings_client_id`/`_secret`) posé à la main dans OpenBao, image 0.6.0 GHCR
      multi-arch, manifeste tantive bumpé (env + externalSecrets + routes d'enrôlement,
      réplique unique commentée). Vérifié live : `/health` → 4 addons `ok` en 0.6.0,
      ExternalSecret `SecretSynced` avec les 5 clés, `/withings/enroll` → 302 SSO,
      `/withings/` sans token → 401
- [ ] Alfred (cerveau) : consigne d'usage des mesures Withings — quels outils, quelle
      fenêtre par défaut, et le fait que « manual: true » n'est pas une mesure
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
