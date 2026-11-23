<h1 align="center">MYA-MCP</h1>

<p align="center">
  Un serveur <a href="https://modelcontextprotocol.io">MCP</a> pour explorer les destinations internationales de <a href="https://mya.epitech.eu">mya.epitech.eu</a> depuis votre assistant IA.
</p>

<p align="center">
  <a href="LICENSE"><img alt="Licence MIT" src="https://img.shields.io/badge/licence-MIT-green.svg"></a>
  <img alt="Python 3.12" src="https://img.shields.io/badge/python-3.12-blue.svg">
  <img alt="MCP streamable HTTP" src="https://img.shields.io/badge/MCP-streamable%20HTTP-8A2BE2.svg">
  <img alt="OAuth 2.1" src="https://img.shields.io/badge/auth-OAuth%202.1-orange.svg">
  <img alt="Docker" src="https://img.shields.io/badge/docker-pr%C3%AAt-2496ED.svg">
</p>

MYA-MCP récupère chaque jour les fiches des universités partenaires publiées sur MYA, les indexe dans une base SQLite (FTS5) et les expose à un assistant IA : recherche plein texte, filtres structurés, fiches complètes, comparaison et suivi des changements. Il s'utilise comme **connecteur personnalisé de Claude.ai**, dans **Claude Code** ou avec tout client MCP compatible.

Exemples de questions que l'on peut ensuite poser à son assistant :

- « Quelles destinations en Pologne sont sans surcoût ? »
- « Compare AGH, Gdansk et Wroclaw : places, GPA, semestre. »
- « Trouve une université anglophone avec une spécialisation en sécurité et un GPA exigé inférieur à 12. »
- « Qu'est-ce qui a changé sur MYA cette semaine ? »

> Projet indépendant, non affilié à Epitech. Il s'appuie sur une API non documentée officiellement (voir [Limites](#limites)).

## Sommaire

- [Fonctionnalités](#fonctionnalités)
- [Démarrage rapide](#démarrage-rapide-docker)
- [Architecture](#architecture)
- [Variables d'environnement](#variables-denvironnement)
- [Référence des outils MCP](#référence-des-outils-mcp)
- [Connecter à Claude.ai](#connecter-à-claudeai) · [à Claude Code](#connecter-à-claude-code) · [à un autre client](#autres-clients-mcp)
- [Déploiement en production](#déploiement-en-production)
- [Refresh quotidien et historique des changements](#refresh-quotidien-et-historique-des-changements)
- [Sécurité](#sécurité)
- [Sauvegarde du volume](#sauvegarde-du-volume)
- [Dépannage](#dépannage)
- [Développement local et tests](#développement-local-et-tests)
- [Contribuer](#contribuer)
- [Limites](#limites)
- [Licence](#licence)

## Fonctionnalités

- Transport MCP **streamable HTTP**, endpoint `/mcp` (SDK Python officiel `mcp`, FastMCP).
- **OAuth 2.1** intégré : métadonnées de découverte (RFC 9728 et RFC 8414), enregistrement dynamique des clients (RFC 7591), PKCE S256, refresh tokens avec rotation, révocation. Compatible avec les connecteurs personnalisés de Claude.ai et avec Claude Code.
- Mono-utilisateur : une page `/consent` demande le mot de passe du serveur (`MCP_PASSWORD`), avec protection CSRF et limitation des essais.
- 6 outils : `list_destinations`, `get_destination`, `search_destinations`, `list_filters`, `compare_destinations`, `get_data_status`, plus une ressource `mya://about`.
- Recherche plein texte SQLite **FTS5** insensible aux accents et à la casse (`unicode61 remove_diacritics 2`), classement bm25, extraits (snippets), pluriels et préfixes simples, requête assainie (aucune syntaxe FTS injectable), repli sur `LIKE`.
- Filtres : pays, langue, Erasmus, semestre, GPA maximal, diplôme, spécialisation, surcoût maximal.
- Refresh automatique chaque jour dans le même process, et au démarrage si la base est vide ou a plus de 24 h.
- Écriture atomique : la nouvelle base est construite à côté puis substituée ; en cas d'échec du scrape (réseau, 0 destination, chute anormale), l'ancienne base est conservée.
- Historique des changements (ajouts, suppressions, modifications de champs) entre deux scrapes.
- `/health` public sans données pour le healthcheck Docker.
- Image Docker `python:3.12-slim`, utilisateur non root, `HEALTHCHECK`, volume `/data`.

## Architecture

```
  mya.epitech.eu/api                  Claude.ai / Claude Code / client MCP
  GET /universities                              |
  GET /universities/{id}                         | HTTPS (reverse proxy obligatoire en production)
          ^                                      v
          | httpx (retries, backoff)    +-------------------------------+
          |                             |  Conteneur mya-mcp            |
  +-------+---------+  validation +     |                               |
  |  scraper.py     |  swap atomique    |  server.py (FastMCP)          |
  |  scheduler.py   +-----------------> |   /mcp          (OAuth requis)|
  |  (refresh 1x/j) |                   |   /health       (public)      |
  +-----------------+                   |  oauth.py (serveur OAuth)     |
                                        |   /.well-known/* /register    |
                                        |   /authorize /consent /token  |
                                        |   /revoke                     |
                                        |  db.py (SQLite + FTS5)        |
                                        +---------------+---------------+
                                                        |
                                    volume /data : mya.db (données)
                                                   oauth.db (clients, tokens hachés)
```

Structure du dépôt :

```
mya_mcp/
  config.py      variables d'environnement et validation
  scraper.py     API MYA, retries, HTML vers texte, normalisation
  db.py          schéma SQLite/FTS5, swap atomique, requêtes, changements
  scheduler.py   refresh, garde-fous, planification quotidienne
  oauth.py       serveur d'autorisation OAuth, page de consentement, stockage des tokens
  server.py      outils MCP, intégration OAuth, /health
tests/           tests hors réseau (pytest)
Dockerfile, docker-compose.yml, requirements.txt, .env.example
```

## Prérequis

- Docker (et Docker Compose v2) pour l'usage normal.
- Un accès sortant vers `https://mya.epitech.eu`.
- **Pour Claude.ai** : un nom de domaine en HTTPS, joignable depuis Internet, qui pointe vers le conteneur via un reverse proxy (voir [Connecter à Claude.ai](#connecter-à-claudeai)). Claude.ai contacte le serveur depuis les serveurs d'Anthropic, pas depuis votre navigateur : `localhost` ou un réseau privé ne suffisent pas.
- Pour le développement sans Docker : Python 3.12.

## Démarrage rapide (Docker)

Avec Docker Compose :

```bash
git clone https://github.com/Michael-1103/mya-mcp.git && cd mya-mcp
export MCP_PASSWORD=$(openssl rand -hex 32)
export PUBLIC_URL=https://mya-mcp.exemple.org     # l'URL publique finale (https, sans chemin)
echo "Mot de passe : $MCP_PASSWORD"                # à noter, il sera demandé à la connexion
docker compose up -d --build
docker compose logs -f mya-mcp                      # le premier scrape dure environ 10 secondes
curl http://localhost:8000/health
```

Avec Docker seul :

```bash
docker build -t mya-mcp .
docker run -d --name mya-mcp --restart unless-stopped \
  -e MCP_PASSWORD="$(openssl rand -hex 32)" \
  -e PUBLIC_URL=https://mya-mcp.exemple.org \
  -p 8000:8000 -v mya-data:/data mya-mcp
```

Le serveur écoute sur `0.0.0.0:8000` et sert MCP sur `/mcp`. Il refuse de démarrer si `MCP_PASSWORD` ou `PUBLIC_URL` manque. Réponse attendue de `/health` :

```json
{"status":"ok","version":"1.0.0","destinations":138,"data_age_seconds":19,"last_success_at":"2026-11-20T14:33:27+00:00"}
```

`status` vaut `ok`, `stale` (données de plus de `MAX_AGE_HOURS` heures) ou `no_data` (premier scrape pas encore terminé). Le code HTTP reste 200 tant que le process vit, afin qu'une panne de la source ne fasse pas redémarrer le conteneur en boucle.

Pour un essai local rapide, sans authentification et sans exposer le port (jamais en production) : `-e ALLOW_NO_AUTH=1` à la place de `MCP_PASSWORD` et `PUBLIC_URL`.

## Variables d'environnement

| Variable | Défaut | Description |
|---|---|---|
| `MCP_PASSWORD` | (aucun) | **Obligatoire** (12 caractères minimum). Mot de passe demandé sur la page de connexion OAuth (`/consent`). Ce n'est pas un token à mettre dans un en-tête : il sert uniquement à autoriser un client. |
| `PUBLIC_URL` | (aucun) | **Obligatoire.** URL publique du serveur, en `https`, sans chemin ni slash final (ex. `https://mya-mcp.exemple.org`). Sert d'issuer OAuth et de base de la ressource MCP (`<PUBLIC_URL>/mcp`). `http` n'est accepté que pour `localhost`. Elle doit correspondre exactement à l'URL que saisissent les clients. |
| `ALLOW_NO_AUTH` | `0` | `1` désactive toute authentification et rend `MCP_PASSWORD` et `PUBLIC_URL` facultatifs (test local uniquement). |
| `FORWARDED_ALLOW_IPS` | `*` | IP des reverse proxys dont on accepte `X-Forwarded-For` et `X-Forwarded-Proto` (liste séparée par des virgules, ou `*`). Voir [Sécurité](#sécurité). |
| `DATA_DIR` | `/data` | Dossier des bases SQLite `mya.db` (données) et `oauth.db` (clients et tokens). À monter sur un volume persistant. |
| `PORT` | `8000` | Port d'écoute dans le conteneur. |
| `HOST` | `0.0.0.0` | Adresse d'écoute. |
| `REFRESH_HOUR_UTC` | `3` | Heure UTC (0-23) du refresh quotidien. |
| `MAX_AGE_HOURS` | `24` | Âge au-delà duquel les données sont considérées périmées (refresh au démarrage). |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` ou `ERROR`. |
| `MAX_DROP_PERCENT` | `30` | Un scrape qui ferait chuter le nombre de destinations de plus de ce pourcentage est rejeté. |
| `MAX_FAILED_PERCENT` | `5` | Pourcentage maximal de fiches détaillées en échec toléré. Au-delà, le scrape est rejeté. |
| `RETRY_DELAY_MINUTES` | `30` | Délai avant un nouvel essai après un scrape en échec. |
| `SCRAPE_CONCURRENCY` | `8` | Nombre de requêtes simultanées vers l'API MYA. |
| `MYA_API_BASE` | `https://mya.epitech.eu/api` | URL de base de l'API (utile pour des tests). |
| `MYA_SITE_BASE` | `https://mya.epitech.eu` | Base des URLs publiques des fiches. |

Un fichier [`.env.example`](.env.example) est fourni (placeholders uniquement). Avec Docker Compose, un fichier `.env` placé à côté de `docker-compose.yml` est lu automatiquement.

## Générer le mot de passe

```bash
openssl rand -hex 32
```

Cela produit 64 caractères hexadécimaux (256 bits d'aléa). Ne le placez jamais dans le dépôt, dans une image ou dans un message de commit ; passez-le uniquement par variable d'environnement. Pour le changer, modifiez `MCP_PASSWORD` et redémarrez le conteneur : les connexions déjà autorisées continuent de fonctionner. Pour **déconnecter tous les clients**, supprimez `oauth.db` du volume (ou retirez le connecteur côté client puis supprimez le fichier) et redémarrez.

## Référence des outils MCP

Toutes les sorties sont du JSON compact. Les listes et recherches renvoient un **résumé** sans le texte long ; `get_destination` renvoie la fiche complète. Les filtres texte ignorent la casse et les accents. Les erreurs (id inconnu, données pas encore disponibles) sont renvoyées avec `isError: true` et un message explicite.

Champs d'une destination : `id`, `url` (page publique), `university_name`, `country`, `language`, `diploma` (`YES`/`NO`), `spots` (nombre de places **total**), `gpa_requirement` (0 = aucune exigence), `extra_charge_cents` et `extra_charge_eur`, `erasmus` (`YES`/`NO`), `semester` (`Full-year only`, `Full-year or semester`, `Semester only`), `specializations`, `display`, `suspended`, `updated_at`, et `full_text` dans la fiche complète (présentation, démarches administratives, logement, cours, coût de la vie, convertis en texte).

### `list_destinations`

Liste paginée, triée par pays puis par nom.

| Paramètre | Type | Description |
|---|---|---|
| `country` | texte | Pays exact (voir `list_filters`). |
| `language` | texte | Langue d'enseignement (sous-chaîne). |
| `erasmus` | `YES`/`NO` | Programme Erasmus. |
| `semester` | texte | Une des trois valeurs de semestre. |
| `max_gpa` | nombre | GPA exigé inférieur ou égal (0 = sans exigence). |
| `diploma` | `YES`/`NO` | Parcours diplômant. |
| `specialization` | texte | Sous-chaîne d'une spécialisation. |
| `max_extra_charge_eur` | nombre | Surcoût maximal en euros (0 = destinations sans surcoût). |
| `limit` | entier | 1 à 100 (défaut 20). |
| `offset` | entier | Pagination (défaut 0). |

Exemple d'appel : `{"country": "Poland", "limit": 2}`

```json
{"total":1,"offset":0,"count":1,"results":[{"id":121,"url":"https://mya.epitech.eu/university/121","university_name":"AGH University of science and technology","country":"Poland","language":"English","diploma":"NO","spots":5,"gpa_requirement":0.0,"extra_charge_cents":0,"erasmus":"YES","semester":"Full-year or semester","specializations":["Databases","Networks","Software Architecture","Graphics Programming","Security"],"display":"YES","suspended":"NO","updated_at":"2026-11-02T14:02:48.897Z","extra_charge_eur":0.0}]}
```

### `get_destination`

Fiche complète d'une destination.

| Paramètre | Type | Description |
|---|---|---|
| `id` | entier | Identifiant de la destination. |

Exemple d'appel : `{"id": 121}`. Sortie : tous les champs ci-dessus, plus `images` et `full_text` (sections `### Overview`, `### Administrative ...`, `### Accommodation`, `### Courses ...`, `### Cost of living ...`).

### `search_destinations`

Recherche plein texte (noms, pays, spécialisations, textes des fiches), classée par pertinence. Mêmes filtres optionnels que `list_destinations` (sauf `offset`), plus :

| Paramètre | Type | Description |
|---|---|---|
| `query` | texte | Mots de la recherche, sans syntaxe particulière. |
| `limit` | entier | 1 à 50 (défaut 10). |

La sortie contient `mode` (`fts_and` : tous les mots, `fts_or` : au moins un mot, `like` : repli par sous-chaîne), un `score` (plus grand = plus pertinent) et un `snippet` où les termes trouvés sont entourés de `[crochets]`.

Exemple d'appel : `{"query": "cost of living", "country": "Poland", "limit": 1}`

```json
{"query":"cost of living","mode":"fts_and","count":1,"results":[{"id":121,"university_name":"AGH University of science and technology","country":"Poland","score":6.2,"snippet":" ... ### [Cost] [of] [living] ... "}]}
```

(sortie abrégée : les champs de résumé habituels sont aussi présents)

### `list_filters`

Sans paramètre. Renvoie les valeurs distinctes utilisables comme filtres, avec le nombre de destinations : `countries`, `languages`, `semesters`, `erasmus`, `diploma`, `specializations` et `max_gpa_values`.

```json
{"countries":[{"value":"USA","count":18},{"value":"Canada","count":10}],"semesters":[{"value":"Full-year only","count":104}]}
```

(sortie abrégée)

### `compare_destinations`

| Paramètre | Type | Description |
|---|---|---|
| `ids` | liste d'entiers | 2 à 6 identifiants distincts. |

Renvoie `destinations` (résumés) et `differences`, la liste des champs dont les valeurs diffèrent (`country`, `language`, `spots`, `gpa_requirement`, `extra_charge_cents`, `erasmus`, `semester`, `specializations`, `suspended`, `diploma`). Les identifiants introuvables sont listés dans `missing_ids`.

Exemple d'appel : `{"ids": [121, 110, 5]}`

### `get_data_status`

Sans paramètre. Renvoie l'état de fraîcheur des données.

```json
{"last_success_at":"2026-11-20T14:31:11.566024+00:00","data_age_seconds":54,"destinations":138,"last_attempt_at":"2026-11-20T14:31:11.566024+00:00","last_error":null,"refresh_hour_utc":3,"source":"https://mya.epitech.eu","recent_changes":[]}
```

`recent_changes` contient les 20 derniers changements : `{"changed_at", "dest_id", "dest_name", "change_type": "added|removed|modified", "field", "old_value", "new_value"}`.

### Ressource `mya://about`

Courte description du serveur, à titre indicatif.

## Connecter à Claude.ai

Claude.ai (web, Desktop, mobile) ne permet pas de définir un en-tête `Authorization` statique pour un connecteur personnalisé : il utilise soit aucune authentification, soit OAuth. Ce serveur implémente donc OAuth 2.1 avec découverte automatique, enregistrement dynamique du client (DCR) et PKCE S256, conformément à la [documentation des connecteurs](https://claude.com/docs/connectors/building/authentication) et à la spécification MCP.

### Prérequis

1. **Un domaine en HTTPS public**, joignable depuis Internet (Claude.ai contacte votre serveur depuis les serveurs d'Anthropic). Un certificat valide est nécessaire ; un reverse proxy (Caddy, Traefik, Nginx...) termine le TLS et transmet vers le port 8000 du conteneur, en conservant les en-têtes `X-Forwarded-*` et les chemins `/.well-known/*`, `/authorize`, `/consent`, `/token`, `/register`, `/revoke`, `/mcp`, `/health`.
2. Le conteneur lancé avec `PUBLIC_URL=https://<domaine>` (exactement l'URL que vous saisirez dans Claude, sans chemin) et `MCP_PASSWORD`.
3. Aucun filtrage IP, WAF ou authentification devant le serveur (les requêtes OAuth de Claude doivent passer ; au besoin, autorisez la plage `160.79.104.0/21` utilisée par Anthropic pour ses requêtes sortantes).

Vérification avant d'aller plus loin :

```bash
curl -s https://<domaine>/health
curl -si -X POST https://<domaine>/mcp | head -5     # attendu : 401 avec un en-tête WWW-Authenticate: Bearer ... resource_metadata="https://<domaine>/.well-known/oauth-protected-resource/mcp"
curl -s https://<domaine>/.well-known/oauth-protected-resource/mcp
curl -s https://<domaine>/.well-known/oauth-authorization-server
```

Les URLs de ces deux documents doivent commencer par `https://<domaine>`. Si elles affichent `http://` ou un autre hôte, vérifiez `PUBLIC_URL` et les en-têtes transmis par le proxy.

### Ajouter le connecteur

1. Dans Claude.ai : **Paramètres > Connecteurs > Ajouter un connecteur personnalisé** (l'intitulé exact peut varier selon l'offre ; sur les offres Team et Enterprise, un propriétaire de l'organisation doit l'ajouter).
2. Nom : `MYA` (libre). URL du serveur MCP : `https://<domaine>/mcp`.
3. Laissez les champs avancés (ID et secret client OAuth) **vides** : Claude s'enregistre automatiquement (DCR).
4. Cliquez sur **Ajouter**, puis **Connecter**. Une page s'ouvre sur votre domaine : « Autoriser l'accès aux destinations MYA ». Saisissez `MCP_PASSWORD` et cliquez sur **Autoriser**.
5. Vous revenez dans Claude, le connecteur est actif. Activez-le dans une conversation (menu des connecteurs) et demandez par exemple : « Quelles destinations en Pologne sont sans surcoût ? ».

Les tokens d'accès durent 1 h et sont renouvelés automatiquement avec le refresh token (rotation à chaque renouvellement, validité 60 jours d'inactivité maximum). Les clients et tokens sont stockés dans `/data/oauth.db` (tokens hachés) et survivent aux redémarrages.

### Dépannage de la connexion Claude.ai

| Message ou symptôme | Cause probable et correction |
|---|---|
| « Couldn't reach the MCP server » | Le serveur n'est pas joignable depuis Internet, ou le 401 ne contient pas `resource_metadata`, ou `/.well-known/*` renvoie 404 via le proxy. Refaites les vérifications `curl` ci-dessus depuis une machine extérieure. |
| « Authorization with the MCP server failed » | Le flux a échoué après la page de connexion. Consultez `docker logs` ; vérifiez que `/token` et `/register` répondent en moins de 10 secondes et que le proxy ne bloque pas les `POST` en `application/x-www-form-urlencoded` ou `application/json`. |
| Les URLs des métadonnées commencent par `http://` | `PUBLIC_URL` est en `http`, ou le proxy ne transmet pas `X-Forwarded-Proto`. Utilisez `PUBLIC_URL=https://...` et vérifiez `FORWARDED_ALLOW_IPS`. |
| Le serveur refuse de démarrer (`PUBLIC_URL est obligatoire`) | Définir `PUBLIC_URL` en `https` sans chemin ni slash final. |
| « Mot de passe incorrect » | C'est `MCP_PASSWORD`, pas un autre secret. Après 5 échecs depuis une même IP (ou 5 sur une même demande), le formulaire se bloque 15 minutes ; relancez la connexion depuis Claude. |
| « Demande expirée » | La page de connexion est valable 10 minutes. Relancez la connexion depuis Claude. |
| Erreur `invalid_redirect_uri` à l'enregistrement | Seuls `https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback` et les adresses de boucle locale (`http://localhost:<port>/...`, `http://127.0.0.1:<port>/...`) sont acceptés. |
| Plus de réponse après quelques jours | Le refresh token a pu expirer ou `oauth.db` a été supprimé : retirez puis rajoutez le connecteur. |

Si vous préférez une organisation qui injecte un en-tête statique (fonction en bêta pour certaines organisations, voir la documentation Claude), ce serveur ne l'accepte volontairement pas : OAuth est son seul mode d'authentification.

## Connecter à Claude Code

Claude Code utilise aussi OAuth, avec une redirection sur `http://localhost:<port>/callback` (acceptée) :

```bash
claude mcp add --transport http mya https://<domaine>/mcp
```

Puis, dans une session Claude Code, tapez `/mcp`, choisissez `mya` et **Authenticate** : le navigateur s'ouvre sur la page de connexion de votre serveur, saisissez `MCP_PASSWORD`. Aucun en-tête `--header` n'est nécessaire (un en-tête `Authorization` statique n'est pas accepté par ce serveur).

## Autres clients MCP

Tout client MCP qui supporte le transport streamable HTTP et l'autorisation MCP (OAuth 2.1, découverte RFC 9728, DCR, PKCE S256) convient : URL `https://<domaine>/mcp`. Les redirections acceptées à l'enregistrement sont limitées aux callbacks Claude et à la boucle locale. Un client qui n'utiliserait qu'un redirect `https` vers un autre domaine sera refusé.

## Déploiement en production

Le serveur doit être joignable en HTTPS sur un domaine public (Claude.ai l'appelle depuis les serveurs d'Anthropic). Le principe est toujours le même : un conteneur, un volume sur `/data`, un reverse proxy qui termine le TLS vers le port 8000.

Exemple avec Docker Compose et Caddy (`Caddyfile` minimal) :

```
mya-mcp.exemple.org {
    reverse_proxy mya-mcp:8000
}
```

Ne publiez pas le port 8000 en clair sur Internet (voir [Sécurité](#sécurité)).

## Refresh quotidien et historique des changements

- **Au démarrage** : si la base n'existe pas ou date de plus de `MAX_AGE_HOURS` heures, un scrape est lancé en arrière-plan (le serveur répond déjà, `/health` indique `no_data` tant que ce n'est pas terminé).
- **Chaque jour** à `REFRESH_HOUR_UTC` (heure UTC).
- **Scrape** : `GET /universities` puis `GET /universities/{id}` pour chaque destination (concurrence 8, timeout 30 s, jusqu'à 4 tentatives avec backoff exponentiel sur erreurs réseau, 429 et 5xx). Toute erreur est journalisée.
- **Garde-fous** : le scrape est rejeté, et l'ancienne base conservée, s'il ne renvoie aucune destination, si trop de fiches échouent (`MAX_FAILED_PERCENT`), ou si le nombre de destinations chute de plus de `MAX_DROP_PERCENT`. L'échec est enregistré (`last_error` dans `get_data_status`) et un nouvel essai a lieu après `RETRY_DELAY_MINUTES`. Quelques fiches isolées en échec reprennent leur ancienne version.
- **Écriture atomique** : une base `mya.db.new` est construite, vérifiée, puis substituée à `mya.db` par `os.replace`. Les lectures en cours ne voient jamais d'état partiel.
- **Historique** : table `changes` (date, destination, type `added`/`removed`/`modified`, champ modifié, ancienne et nouvelle valeur raccourcies). Rien n'est enregistré au tout premier chargement ; les 2000 derniers changements sont conservés. Pour `full_text`, seul le fait que le texte a changé est noté. Consultable via `get_data_status`.

## Sécurité

- **Authentification** : OAuth 2.1 uniquement. Il n'existe aucun token statique : le mot de passe n'est accepté que sur la page `/consent`. Il est comparé en temps constant (empreintes SHA-256 et `hmac.compare_digest`).
- **Page de connexion** : jeton CSRF lié à la demande et cookie `SameSite=Strict`, échappement HTML des valeurs affichées, en-têtes `X-Frame-Options: DENY`, `Content-Security-Policy`, `Cache-Control: no-store`. Elle affiche le nom du client et l'hôte de redirection.
- **Limitation des essais** : 5 mots de passe faux par demande, 5 échecs par IP puis blocage de 15 minutes, et un plafond global (50 échecs sur 15 minutes) qui limite les contournements par fausses IP.
- **Codes et tokens** : code d'autorisation à usage unique, valable 5 minutes, lié au `code_challenge` PKCE (S256 uniquement) et à la `redirect_uri` ; sa réutilisation révoque les tokens émis. Access tokens de 1 h, refresh tokens avec rotation (l'ancien est invalidé). Tokens opaques, stockés hachés (SHA-256) dans `oauth.db`.
- **Redirections** : l'enregistrement dynamique n'accepte que le callback Claude (`https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback`) et la boucle locale ; `/authorize` exige ensuite une correspondance exacte avec l'URI enregistrée. L'enregistrement est ouvert (c'est le principe du DCR) mais n'ouvre aucun accès sans le mot de passe ; le nombre de clients est plafonné et les clients inutilisés sont purgés.
- **Ne pas exposer le serveur sans TLS** : placez un reverse proxy HTTPS devant le port 8000 et ne publiez pas ce port en clair sur Internet. `PUBLIC_URL` en `http` n'est accepté que pour `localhost`.
- **Reverse proxy et `FORWARDED_ALLOW_IPS`** : le serveur lit `X-Forwarded-For` et `X-Forwarded-Proto` pour connaître l'IP du client (limitation des essais) et le schéma. Avec la valeur par défaut `*`, tout appelant pourrait falsifier ces en-têtes s'il atteint le conteneur directement : ne publiez donc le port que vers votre proxy (réseau Docker interne), ou restreignez `FORWARDED_ALLOW_IPS` à l'IP du proxy.
- Les données servies sont publiques (fiches du site MYA) ; l'authentification protège surtout contre l'usage abusif de votre instance. `/health` ne renvoie que l'état, le nombre de destinations et l'âge des données.
- Le conteneur tourne avec un utilisateur non root. La requête de recherche est assainie : seuls des mots entre guillemets sont transmis à FTS5.
- Changez `MCP_PASSWORD` si vous le soupçonnez compromis, et supprimez `oauth.db` pour révoquer tous les accès existants.

## Sauvegarde du volume

La seule donnée à conserver est `mya.db`, mais elle se reconstruit entièrement par un scrape (seul l'historique des changements serait perdu). Sauvegarde d'un volume nommé :

```bash
docker run --rm -v mya-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/mya-data.tar.gz -C /data .
```

Restauration : même commande avec `tar xzf` vers un volume vide. Le nom réel du volume avec Compose est préfixé par le nom du projet (`docker volume ls`).

## Dépannage

Pour les erreurs de connexion de Claude.ai, voir [Dépannage de la connexion Claude.ai](#dépannage-de-la-connexion-claudeai).

| Symptôme | Piste |
|---|---|
| Le conteneur s'arrête au démarrage avec `MCP_PASSWORD est obligatoire` ou `PUBLIC_URL est obligatoire` | Définir les deux variables (ou `ALLOW_NO_AUTH=1` en local). |
| `401` sur `/mcp` | Normal sans token : le client doit passer par OAuth (le 401 contient `WWW-Authenticate` avec `resource_metadata`). Un token `Authorization: Bearer <MCP_PASSWORD>` n'est pas accepté. |
| `/health` renvoie `no_data` | Premier scrape en cours ou en échec ; voir `docker logs`. |
| `/health` renvoie `stale` | Le refresh échoue depuis plus de `MAX_AGE_HOURS` ; consulter `last_error` via `get_data_status` et les logs. L'ancienne base reste servie. |
| `unable to open database file` / erreurs de droits | Le dossier `/data` doit être inscriptible par l'UID 10001. Avec un bind mount : `chown 10001 <dossier>`. Un volume nommé n'a pas ce souci. |
| Le client ne voit pas les outils | Vérifier l'URL (elle doit finir par `/mcp`), le transport `http` (streamable HTTP) et que la connexion OAuth a abouti. |

Les logs (`docker logs mya-mcp`) indiquent chaque refresh, ses échecs, les mots de passe incorrects (avec l'IP) et la date du prochain refresh.

## Développement local et tests

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q tests                       # tests hors réseau

# lancer le serveur localement (sans Docker), avec OAuth sur localhost
DATA_DIR=./data MCP_PASSWORD=mot-de-passe-local-123 PUBLIC_URL=http://localhost:8000 python -m mya_mcp
# ou sans aucune authentification (jamais exposé)
DATA_DIR=./data ALLOW_NO_AUTH=1 python -m mya_mcp
```

Les tests couvrent la conversion HTML, la recherche et l'assainissement des requêtes, l'historique des changements, les scénarios d'échec du scrape (ancienne base conservée) et tout le flux OAuth : métadonnées, enregistrement, consentement (mauvais et bon mot de passe, CSRF, limitation), PKCE correct et incorrect, réutilisation et expiration du code, refresh avec rotation, révocation, expiration, accès à `/mcp` avec un token OAuth, et réponses `401` avec `WWW-Authenticate`. Le dossier `data/` est ignoré par git.

Le SDK `mcp` est épinglé en 1.x (`mcp==1.30.0`) : la branche 2.x a renommé `FastMCP` en `MCPServer` et n'est pas supportée par ce code. Le module `oauth.py` applique deux petits ajustements au SDK (champ `client_secret` rendu optionnel pour `/revoke`, et méthode d'authentification `none` annoncée dans les métadonnées) ; à revérifier lors d'une mise à jour du SDK.

## Contribuer

Les contributions sont les bienvenues : corrections, nouveaux filtres, documentation. Ouvrez une issue avant un changement important, une pull request par sujet, avec des tests. Les règles détaillées sont dans [CONTRIBUTING.md](CONTRIBUTING.md).

## Limites

- L'API de mya.epitech.eu n'est pas une API publique documentée : son format ou son accès peuvent changer sans préavis, ce qui casserait le scrape (les garde-fous conservent alors les dernières données valides).
- `spots` est le nombre de places **total** de la destination, pas le nombre de places restantes.
- Les données ne sont fraîches qu'au rythme du refresh (quotidien). Les textes des fiches sont en anglais ou en français selon la destination.
- Le nombre de destinations évolue avec le site (138 en novembre 2026).
- Aucune donnée personnelle ni authentification Epitech : seules les informations publiques du site sont lues.
- Un seul utilisateur et un seul mot de passe : pas de comptes multiples ni de gestion de rôles. Pas de page d'administration des clients autorisés (pour tout révoquer : supprimer `oauth.db`).
- Respectez le site source : un scrape par jour avec 8 requêtes simultanées suffit.

## Licence

Distribué sous licence [MIT](LICENSE).
