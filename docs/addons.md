# Rosetta — addons

Un addon est un module de `src/rosetta/addons/` (hors fichiers préfixés `_`), monté sous `/<nom>`. Ce fichier a une section par addon, dans l'ordre alphabétique. Les noms d'outils sont ceux de l'instantané `tests/surface.json` (vérifié par `tests/test_surface.py`) ; les variables d'environnement, celles de la même source. Leur signification générale est dans la table de configuration du `README.md`. Le **mode d'identité** (`identity`) : `machine` = tout jeton authentifié par le hub est admis ; `user` = les jetons machine sont refusés (403) et chaque appel porte un sujet humain. Les formats des fichiers d'état sont dans `docs/donnees.md`.

| Addon | Identité | Variables requises | État sur disque |
|---|---|---|---|
| [`courrier`](#courrier) | `user` | `MAIL_IMAP_HOST`, `MAIL_DOMAIN` | — |
| [`food`](#food) | `machine` | — | — |
| [`git`](#git) | `user` | — | — |
| [`github`](#github) | `user` | `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | `ROSETTA_GITHUB_DATA` |
| [`google`](#google) | `user` | — | `ROSETTA_GOOGLE_DATA` |
| [`maps`](#maps) | `machine` | `GOOGLE_MAPS_API_KEY` | — |
| [`marees`](#marees) | `machine` | `API_MAREE_KEY` | — |
| [`meteo`](#meteo) | `machine` | — | — |
| [`postier`](#postier) | `machine` | `POSTIER_FROM`, `POSTIER_PASSWORD` | — |
| [`quotas`](#quotas) | `machine` | — | `ROSETTA_QUOTAS_DATA` |
| [`trace`](#trace) | `machine` | — | — |
| [`transit`](#transit) | `machine` | `SNCF_API_KEY`, `IDFM_API_KEY` | — |
| [`verbatim`](#verbatim) | `machine` | — | — |
| [`withings`](#withings) | `user` | `WITHINGS_CLIENT_ID`, `WITHINGS_CLIENT_SECRET` | `ROSETTA_WITHINGS_DATA` |

## courrier

Les boîtes mail propres de la maison (OVH Zimbra, `@<MAIL_DOMAIN>`) : lecture, brouillons, alias jetables. Pas d'envoi ni de suppression de mail. Distinct de `google` (Gmail).

- **Identité** : `user`
- **Variables requises** : `MAIL_IMAP_HOST`, `MAIL_DOMAIN`
- **Autres variables** : optionnelles : `MAIL_DRAFTS_FOLDER` (défaut `Drafts`), `MAIL_VAULT_ADDR`, `MAIL_VAULT_MOUNT` (défaut `jwt-authelia`), `MAIL_VAULT_ROLE` (défaut `rosetta-mail`) pour le mot de passe via le coffre, `OVH_APPLICATION_KEY`, `OVH_APPLICATION_SECRET`, `OVH_CONSUMER_KEY` pour les alias.
- **État** : Aucun : le mot de passe de la boîte est obtenu du coffre à chaque appel (cache mémoire), rien n'est écrit sur disque.

| Outil | Rôle |
|---|---|
| `courrier_alias_creer` | Crée un alias jetable `<nom_local>@<domaine>` vers la boîte de l'appelant (anti-spam : un alias par marchand, à supprimer au premier abus via courrier_alias_supprimer). |
| `courrier_alias_liste` | Liste les alias jetables qui pointent vers la boîte @<domaine familial> de l'appelant. |
| `courrier_alias_supprimer` | Supprime un alias jetable de l'appelant (le marchand a vendu l'adresse ? |
| `courrier_brouillon` | Dépose un BROUILLON dans la boîte @<domaine familial> de l'appelant — PAS dans Gmail (pour Gmail : `mail_draft` de l'addon `google`). |
| `courrier_dossiers` | Liste les dossiers de la boîte PERSONNELLE @<domaine familial> de l'appelant, la messagerie de la maison (INBOX, Sent, Drafts…). |
| `courrier_lire` | Lit un message complet (en-têtes + corps texte) par son `uid`, dans la boîte @<domaine familial> — pas Gmail. |
| `courrier_recherche` | Cherche dans la boîte PERSONNELLE @<domaine familial> de l'appelant — la messagerie de la maison, PAS Gmail (Gmail = `mail_search` de l'addon `google`). |

## food

Open Food Facts en lecture seule : fiche produit par code-barres, recherche texte. Aucune clé, aucun compte.

- **Identité** : `machine`
- **Variables requises** : aucune
- **Autres variables** : Optionnelle : `OFF_USER_AGENT` (défaut `Alfred/1.0 (contact@antor.fr)`).
- **État** : Aucun.

| Outil | Rôle |
|---|---|
| `food_product` | Fiche produit alimentaire à partir d'un ou plusieurs codes-barres (Open Food Facts). |
| `food_search` | Recherche de produits alimentaires par texte libre (Open Food Facts). |

## git

Proxy git smart-HTTP : les pods poussent du vrai git vers GitHub sans détenir de credential GitHub ; le hub utilise le jeton de l'addon `github` et applique l'identité, la liste blanche de dépôts et les règles de refs.

- **Identité** : `user`
- **Variables requises** : aucune
- **Autres variables** : optionnelles : `ROSETTA_GIT_REPOS` (liste `owner/name` séparée par des virgules ; vide = tous les dépôts accessibles à l'App), `ROSETTA_EXTERNAL_URL`.
- **État** : Aucun fichier propre : il lit l'enrôlement de `github`.
- **Routes** : `POST /git/{repo:path}/git-receive-pack`, `POST /git/{repo:path}/git-upload-pack`, `GET /git/{repo:path}/info/refs`

| Outil | Rôle |
|---|---|
| `git_remote` | L'URL de remote à donner à git pour pousser ce dépôt via le hub. |

## github

GitHub (lecture de dépôts, PR, runs, issues ; écritures bornées : commit, tag, création de dépôt, fusion de PR) au nom de l'appelant, via une GitHub App enrôlée par utilisateur.

- **Identité** : `user`
- **Variables requises** : `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`
- **Autres variables** : optionnelles : `GITHUB_OWNER`, `ROSETTA_EXTERNAL_URL` (URL de callback OAuth), `ROSETTA_GITHUB_DATA` (défaut `/data/github`).
- **État** : variable `ROSETTA_GITHUB_DATA` (défaut `/data/github`) ; fichiers sous `$ROSETTA_GITHUB_DATA/` — voir `docs/donnees.md`.
- **Routes** : `GET /github/callback`, `GET /github/enroll`

| Outil | Rôle |
|---|---|
| `actions_runs` | Derniers runs GitHub Actions d'un dépôt : conclusion, branche, date. |
| `issue_comment` | Ajoute un commentaire à une ISSUE. Ni ouvrir, ni fermer, ni étiqueter. |
| `pull_request` | Détail d'UNE pull request : de quoi la JUGER avant de la fusionner. |
| `pull_request_merge` | Fusionne une pull request — la SEULE écriture de PR de cet addon. |
| `pull_requests` | Liste les pull requests d'un dépôt, la plus récemment mise à jour d'abord. |
| `repo_commit` | Publie UN commit atomique sur `origin`. |
| `repo_commits` | Derniers commits d'un dépôt, ou d'un fichier si `chemin` est fourni. |
| `repo_create` | Crée un dépôt NEUF : privé, vide, sous le compte GitHub de l'appelant. |
| `repo_file` | Lit un fichier texte d'un dépôt. `ref` = branche, tag ou sha (défaut : branche par défaut). |
| `repo_list` | Liste les dépôts accessibles (nom, description, branche par défaut, visibilité). |
| `repo_search_code` | Recherche de code. `repo` restreint à un dépôt, sinon toute la flotte accessible. |
| `repo_tag` | Pose un tag — le geste qui déclenche la CI, donc une release. |
| `repo_tags` | Tags d'un dépôt, du plus récent au plus ancien — utile pour connaître la dernière version publiée. |
| `repo_tree` | Arborescence d'un dépôt à un ref donné (récursive, fichiers seuls). |

## google

Gmail et Google Agenda au nom de l'appelant : lecture, brouillons, envoi, événements.

- **Identité** : `user`
- **Variables requises** : aucune
- **Autres variables** : Aucune variable obligatoire : le client OAuth se lit dans le fichier `client_secret.json` du répertoire de données (sans lui, l'addon répond « non provisionné »). Optionnelles : `ROSETTA_GOOGLE_DATA` (défaut `/data/google`), `ROSETTA_EXTERNAL_URL`, `ROSETTA_GMAIL_ACCOUNT` (défaut `0`, compte du navigateur pour les liens), `TZ`.
- **État** : variable `ROSETTA_GOOGLE_DATA` (défaut `/data/google`) ; fichiers sous `$ROSETTA_GOOGLE_DATA/` — voir `docs/donnees.md`.
- **Routes** : `GET /google/callback`, `GET /google/enroll`

| Outil | Rôle |
|---|---|
| `calendar_create` | Crée un événement dans un agenda (sur demande explicite de l'utilisateur). |
| `calendar_delete` | SUPPRIME un événement d'un agenda — irréversible : confirmation explicite de l'utilisateur requise en amont. |
| `calendar_events` | Liste les événements entre deux instants, sur un ou plusieurs agendas. |
| `calendar_list` | Liste les agendas du compte : identifiant, nom, et si on peut y écrire. |
| `calendar_update` | Modifie un événement existant (déplacement, renommage…) — confirmation utilisateur requise en amont. |
| `mail_attachment` | Lit ou rapatrie les pièces jointes d'un mail Gmail. |
| `mail_draft` | Dépose un BROUILLON dans GMAIL — cet outil n'envoie rien : le brouillon attend sa relecture (l'utilisateur l'envoie depuis Gmail, ou via `mail_send(draft_id=…)` après son feu vert explicite). |
| `mail_draft_update` | Corrige un BROUILLON existant — cet outil n'envoie rien, et rien n'est supprimé. |
| `mail_drafts` | Liste les brouillons Gmail, ou relit l'un d'eux en entier. |
| `mail_search` | Recherche dans GMAIL (syntaxe Gmail : from:, subject:, after:, is:unread…). |
| `mail_send` | ENVOIE un mail via GMAIL — réel, immédiat, irréversible : le destinataire le reçoit. |
| `mail_thread` | Lit un fil GMAIL complet, rendu lisible (expéditeur, date, texte de chaque message). |

## maps

Google Maps Platform derrière une seule clé : temps de trajet, lieux, géocodage, météo.

- **Identité** : `machine`
- **Variables requises** : `GOOGLE_MAPS_API_KEY`
- **Autres variables** : Aucune.
- **État** : Aucun.

| Outil | Rôle |
|---|---|
| `geocode` | Convertit une adresse en coordonnées + adresse normalisée (utile pour désambiguïser). |
| `search_places` | Recherche de lieux (restaurants, hôtels, sites…) par texte libre. |
| `travel_time` | Temps de trajet et distance entre deux lieux, trafic réel pris en compte. |
| `weather_forecast` | Prévisions météo journalières (1 à 10 jours) pour un lieu (adresse ou « lat,lng »). |
| `weather_hourly` | Prévisions météo HEURE PAR HEURE (1 à 24 h) pour un lieu (adresse ou « lat,lng »). |
| `weather_now` | Conditions météo actuelles pour un lieu (adresse en clair ou « lat,lng »). |

## marees

Heures de marée et coefficient pour le littoral français (api-maree.fr), fenêtre J-30 → J+30 ; coefficient non officiel.

- **Identité** : `machine`
- **Variables requises** : `API_MAREE_KEY`
- **Autres variables** : Optionnelle : `TZ` (défaut `Europe/Paris`).
- **État** : Aucun (liste des ports en cache mémoire).

| Outil | Rôle |
|---|---|
| `marees` | Heures des pleines et basses mers, et coefficient, pour un lieu du littoral français. |
| `marees_ports` | Les ports de référence connus autour d'un lieu, avec leur distance. |

## meteo

Vent heure par heure pour un spot de voile, en nœuds (Open-Meteo), plusieurs modèles ; registre de spots nommés. Aucune clé.

- **Identité** : `machine`
- **Variables requises** : aucune
- **Autres variables** : optionnelles : `ROSETTA_WIND_SPOTS` (JSON `{"La Torche": "47.8367,-4.3492"}` ou `{"nom": {"latlng": "…", "note": "…"}}`, relu à chaque appel), `TZ` (défaut `Europe/Paris`).
- **État** : Aucun.

| Outil | Rôle |
|---|---|
| `wind_forecast` | Prévisions de VENT heure par heure pour un spot, en NŒUDS — vent moyen, rafale, ratio de rafale et direction (d'où il vient). |
| `wind_spots` | Les spots de voile enregistrés, avec leurs coordonnées. |

## postier

Envoi de mail par machine depuis la boîte de l'assistant : expéditeur verrouillé, destinataires bornés par liste, plafond horaire, copie dans le dossier des envoyés.

- **Identité** : `machine`
- **Variables requises** : `POSTIER_FROM`, `POSTIER_PASSWORD`
- **Autres variables** : optionnelles : `POSTIER_ALLOWED` (sans défaut : vide = personne n'est joignable), `POSTIER_MAX_PER_HOUR` (défaut `10`), `POSTIER_SMTP_HOST` (défaut `smtp.mail.ovh.net`), `POSTIER_IMAP_HOST` (défaut `imap.mail.ovh.net`), `POSTIER_SENT_FOLDER` (défaut `Sent`).
- **État** : Aucun (fenêtre glissante du plafond en mémoire).

| Outil | Rôle |
|---|---|
| `envoyer_mail` | Envoie un mail DEPUIS la boîte de l'assistant (expéditeur verrouillé). |

## quotas

Compteurs d'usage d'un abonnement Claude : ce qui est consommé, et quand cela se remet à zéro. Un appel machine lit les compteurs du propriétaire désigné.

- **Identité** : `machine`
- **Variables requises** : aucune
- **Autres variables** : Aucune obligatoire. Optionnelles : `ROSETTA_QUOTAS_DATA` (défaut `/data/quotas`), `ROSETTA_QUOTAS_OWNER` (obligatoire dès que plusieurs abonnements sont enrôlés), `ROSETTA_QUOTAS_CLIENTS` (sujets machine admis, séparés par des virgules ; vide = tous), `ROSETTA_CLAUDE_UA`, `ROSETTA_EXTERNAL_URL`, `TZ`.
- **État** : variable `ROSETTA_QUOTAS_DATA` (défaut `/data/quotas`) ; fichiers sous `$ROSETTA_QUOTAS_DATA/` — voir `docs/donnees.md`.
- **Routes** : `GET/POST /quotas/enroll`

| Outil | Rôle |
|---|---|
| `quotas` | Où en sont les compteurs d'usage de l'abonnement : ce qui est consommé, et quand ça se remet à zéro. |
| `quotas_fournisseurs` | Quels abonnements cet addon sait lire, et lesquels sont déjà enrôlés pour l'appelant. |

## trace

Itinéraires à pied ou à vélo sur OpenStreetMap (BRouter) avec profil d'altitude, et points d'intérêt du marcheur (Overpass). La géométrie sort par `GET /trace/geometrie`, pas par le modèle.

- **Identité** : `machine`
- **Variables requises** : aucune
- **Autres variables** : Aucune obligatoire (services publics sans clé). Optionnelles : `BROUTER_URL`, `OVERPASS_URL`, `IGN_ALTI_URL`, `OVERPASS_USER_AGENT`, `ROSETTA_EXTERNAL_URL`.
- **État** : Aucun (l'itinéraire est recalculé, jamais mis en cache).
- **Routes** : `GET /trace/geometrie`

| Outil | Rôle |
|---|---|
| `trace_calcule` | Calcule un itinéraire à pied (ou à vélo) qui SUIT LES CHEMINS, sur données OpenStreetMap. |
| `trace_pois` | Points d'intérêt du MARCHEUR autour d'un lieu, depuis OpenStreetMap. |

## transit

Prochains passages et perturbations SNCF (gares) et RATP/IDFM (métro, RER, bus, tram), via l'API Navitia.

- **Identité** : `machine`
- **Variables requises** : `SNCF_API_KEY`, `IDFM_API_KEY`
- **Autres variables** : Aucune.
- **État** : Aucun.

| Outil | Rôle |
|---|---|
| `metro_departures` | Prochains passages RATP/IDFM (métro, RER, bus, tram) sur une station, avec retard calculé. |
| `metro_disruptions` | Perturbations RATP/IDFM en cours (incident, travaux). Filtrable par station. |
| `train_departures` | Prochains départs SNCF depuis une gare, avec retard calculé (théorique vs temps réel). |
| `train_disruptions` | Perturbations SNCF en cours (grève, travaux, incident). Filtrable par gare. |

## verbatim

Ce qui a été dit dans une vidéo ou un épisode : sous-titres déjà publiés, découpés en phrases horodatées (via `yt-dlp`). Aucune transcription propre.

- **Identité** : `machine`
- **Variables requises** : aucune
- **Autres variables** : Aucune.
- **État** : Aucun (cache mémoire borné).

| Outil | Rôle |
|---|---|
| `verbatim` | Ce qui a été dit dans une vidéo ou un épisode, en phrases horodatées. |
| `verbatim_cherche` | Où un sujet a été abordé dans un média — les passages, et le lien qui s'y rend. |
| `verbatim_fiche` | Ce que la plateforme dit d'un média — sans une ligne de son texte. |

## withings

Données Withings de l'appelant : activité, mesures corporelles, sommeil, séances, appareils.

- **Identité** : `user`
- **Variables requises** : `WITHINGS_CLIENT_ID`, `WITHINGS_CLIENT_SECRET`
- **Autres variables** : optionnelles : `ROSETTA_WITHINGS_DATA` (défaut `/data/withings`), `ROSETTA_EXTERNAL_URL`, `TZ`.
- **État** : variable `ROSETTA_WITHINGS_DATA` (défaut `/data/withings`) ; fichiers sous `$ROSETTA_WITHINGS_DATA/` — voir `docs/donnees.md`.
- **Routes** : `GET /withings/callback`, `GET /withings/enroll`

| Outil | Rôle |
|---|---|
| `withings_activity` | Activité quotidienne Withings : pas, distance, calories, fréquence cardiaque. |
| `withings_devices` | Les appareils Withings du compte : type, modèle, niveau de pile, dernière synchro. |
| `withings_measures` | Mesures Withings : poids, composition corporelle, tension, ECG, SpO2… Chaque pesée (ou prise de tension) rend un GROUPE de mesures prises ensemble — poids, masse grasse, masse musculaire, eau… — daté, avec sa valeur réelle et son unité. |
| `withings_sleep` | Sommeil Withings : une synthèse par nuit (phases, score, respiration, ronflement). |
| `withings_workouts` | Séances de sport enregistrées par Withings (marche, course, vélo, natation…). |
