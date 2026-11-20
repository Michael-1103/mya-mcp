# MYA-MCP

Serveur [MCP](https://modelcontextprotocol.io) (Model Context Protocol) qui expose les destinations internationales partenaires de [mya.epitech.eu](https://mya.epitech.eu) à un assistant IA : recherche plein texte, filtres structurés, fiches complètes, comparaison et suivi des changements.

Les données sont récupérées automatiquement depuis l'API JSON publique de MYA, stockées dans une base SQLite locale (FTS5) et rafraîchies chaque jour. Le serveur tourne dans un seul conteneur Docker, protégé par un token Bearer.

> Projet indépendant, non affilié à Epitech. Il s'appuie sur une API non documentée officiellement (voir [Limites](#limites)).

## Sommaire

- [Fonctionnalités](#fonctionnalités)
- [Architecture](#architecture)
- [Prérequis](#prérequis)
- [Démarrage rapide (Docker)](#démarrage-rapide-docker)
- [Variables d'environnement](#variables-denvironnement)
- [Générer un token sécurisé](#générer-un-token-sécurisé)
- [Référence des outils MCP](#référence-des-outils-mcp)
- [Connexion aux clients MCP](#connexion-aux-clients-mcp)
- [Refresh quotidien et historique des changements](#refresh-quotidien-et-historique-des-changements)
- [Sécurité](#sécurité)
- [Sauvegarde du volume](#sauvegarde-du-volume)
- [Dépannage](#dépannage)
- [Développement local et tests](#développement-local-et-tests)
- [Limites](#limites)
- [Licence](#licence)

## Fonctionnalités

- Transport MCP **streamable HTTP**, endpoint `/mcp` (SDK Python officiel `mcp`, FastMCP).
- 6 outils : `list_destinations`, `get_destination`, `search_destinations`, `list_filters`, `compare_destinations`, `get_data_status`, plus une ressource `mya://about`.
- Recherche plein texte SQLite **FTS5** insensible aux accents et à la casse (`unicode61 remove_diacritics 2`), classement bm25, extraits (snippets), pluriels et préfixes simples, requête assainie (aucune syntaxe FTS injectable), repli sur `LIKE`.
- Filtres : pays, langue, Erasmus, semestre, GPA maximal, diplôme, spécialisation, surcoût maximal.
- Refresh automatique chaque jour dans le même process, et au démarrage si la base est vide ou a plus de 24 h.
- Écriture atomique : la nouvelle base est construite à côté puis substituée ; en cas d'échec du scrape (réseau, 0 destination, chute anormale), l'ancienne base est conservée.
- Historique des changements (ajouts, suppressions, modifications de champs) entre deux scrapes.
- Authentification Bearer obligatoire (comparaison en temps constant), `/health` public sans données pour le healthcheck.
- Image Docker `python:3.12-slim`, utilisateur non root, `HEALTHCHECK`, volume `/data`.

## Architecture

```
  mya.epitech.eu/api                         Client MCP (Claude Code, etc.)
  GET /universities                                   |
  GET /universities/{id}                              | HTTPS (reverse proxy conseillé)
          ^                                           v
          | httpx (retries, backoff)      +---------------------------+
          |                               |  Conteneur mya-mcp        |
  +-------+---------+  validation +       |                           |
  |  scraper.py     |  swap atomique      |  server.py                |
  |  scheduler.py   +-------------------> |   - auth Bearer (/mcp)    |
  |  (refresh 1x/j) |                     |   - /health (public)      |
  +-----------------+                     |   - outils FastMCP        |
                                          |  db.py (SQLite + FTS5)    |
                                          +------------+--------------+
                                                       |
                                              volume /data (mya.db)
```

Structure du dépôt :

```
mya_mcp/
  config.py      variables d'environnement et validation
  scraper.py     API MYA, retries, HTML vers texte, normalisation
  db.py          schéma SQLite/FTS5, swap atomique, requêtes, changements
  scheduler.py   refresh, garde-fous, planification quotidienne
  server.py      outils MCP, middleware d'authentification, /health
tests/           tests hors réseau (pytest)
Dockerfile, docker-compose.yml, requirements.txt, .env.example
```

## Prérequis

- Docker (et Docker Compose v2) pour l'usage normal.
- Un accès sortant vers `https://mya.epitech.eu`.
- Pour le développement sans Docker : Python 3.12.

## Démarrage rapide (Docker)

Avec Docker Compose :

```bash
git clone <url-du-depot> mya-mcp && cd mya-mcp
export MCP_AUTH_TOKEN=$(openssl rand -hex 32)
echo "Token : $MCP_AUTH_TOKEN"        # à noter pour configurer le client
docker compose up -d --build
docker compose logs -f mya-mcp         # le premier scrape dure environ 10 secondes
curl http://localhost:8000/health
```

Avec Docker seul :

```bash
docker build -t mya-mcp .
docker run -d --name mya-mcp --restart unless-stopped \
  -e MCP_AUTH_TOKEN="$(openssl rand -hex 32)" \
  -p 8000:8000 -v mya-data:/data mya-mcp
```

Le serveur écoute sur `0.0.0.0:8000` et sert MCP sur `/mcp`. Réponse attendue de `/health` :

```json
{"status":"ok","version":"1.0.0","destinations":138,"data_age_seconds":19,"last_success_at":"2026-11-20T14:33:27+00:00"}
```

`status` vaut `ok`, `stale` (données de plus de `MAX_AGE_HOURS` heures) ou `no_data` (premier scrape pas encore terminé). Le code HTTP reste 200 tant que le process vit, afin qu'une panne de la source ne fasse pas redémarrer le conteneur en boucle.

Pour un test rapide sans authentification (jamais en production) : `-e ALLOW_NO_AUTH=1` à la place de `MCP_AUTH_TOKEN`.

## Variables d'environnement

| Variable | Défaut | Description |
|---|---|---|
| `MCP_AUTH_TOKEN` | (aucun) | Token Bearer attendu des clients. **Obligatoire** : le serveur refuse de démarrer sans lui, sauf si `ALLOW_NO_AUTH=1`. |
| `ALLOW_NO_AUTH` | `0` | `1` désactive l'authentification (développement local uniquement). |
| `DATA_DIR` | `/data` | Dossier de la base SQLite `mya.db` (à monter sur un volume persistant). |
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

## Générer un token sécurisé

```bash
openssl rand -hex 32
```

Cela produit 64 caractères hexadécimaux (256 bits d'aléa). Ne le placez jamais dans le dépôt, dans une image ou dans un message de commit ; passez-le uniquement par variable d'environnement. Pour le changer, modifiez `MCP_AUTH_TOKEN`, redémarrez le conteneur et mettez à jour la configuration des clients.

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

## Connexion aux clients MCP

Remplacez `<domaine>` par le nom de domaine public du serveur (derrière HTTPS) et `<token>` par la valeur de `MCP_AUTH_TOKEN`. En local sans TLS : `http://localhost:8000/mcp`.

### Claude Code

```bash
claude mcp add --transport http mya https://<domaine>/mcp --header "Authorization: Bearer <token>"
```

Vérification : `claude mcp list`, ou la commande `/mcp` dans une session. Les outils apparaissent sous le nom `mya`.

### Claude Desktop et claude.ai (connecteur distant)

Les connecteurs personnalisés de claude.ai et de Claude Desktop ne permettent pas, à ma connaissance, de définir un en-tête `Authorization: Bearer` arbitraire (ils prévoient l'absence d'authentification ou OAuth). Ce serveur n'implémente pas OAuth. Pour Claude Desktop, le plus simple est de passer par le pont `mcp-remote` dans `claude_desktop_config.json` (configuration non testée dans ce dépôt) :

```json
{
  "mcpServers": {
    "mya": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://<domaine>/mcp", "--header", "Authorization: Bearer ${MYA_TOKEN}"],
      "env": { "MYA_TOKEN": "<token>" }
    }
  }
}
```

### Autre client MCP

Tout client supportant le transport streamable HTTP convient : URL `https://<domaine>/mcp`, en-tête `Authorization: Bearer <token>`. Exemple avec `curl` (le serveur répond en JSON simple, mode sans session) :

```bash
curl -s https://<domaine>/mcp \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_filters","arguments":{}}}'
```

Sans token ou avec un mauvais token, la réponse est `401` avec `WWW-Authenticate: Bearer`.

## Refresh quotidien et historique des changements

- **Au démarrage** : si la base n'existe pas ou date de plus de `MAX_AGE_HOURS` heures, un scrape est lancé en arrière-plan (le serveur répond déjà, `/health` indique `no_data` tant que ce n'est pas terminé).
- **Chaque jour** à `REFRESH_HOUR_UTC` (heure UTC).
- **Scrape** : `GET /universities` puis `GET /universities/{id}` pour chaque destination (concurrence 8, timeout 30 s, jusqu'à 4 tentatives avec backoff exponentiel sur erreurs réseau, 429 et 5xx). Toute erreur est journalisée.
- **Garde-fous** : le scrape est rejeté, et l'ancienne base conservée, s'il ne renvoie aucune destination, si trop de fiches échouent (`MAX_FAILED_PERCENT`), ou si le nombre de destinations chute de plus de `MAX_DROP_PERCENT`. L'échec est enregistré (`last_error` dans `get_data_status`) et un nouvel essai a lieu après `RETRY_DELAY_MINUTES`. Quelques fiches isolées en échec reprennent leur ancienne version.
- **Écriture atomique** : une base `mya.db.new` est construite, vérifiée, puis substituée à `mya.db` par `os.replace`. Les lectures en cours ne voient jamais d'état partiel.
- **Historique** : table `changes` (date, destination, type `added`/`removed`/`modified`, champ modifié, ancienne et nouvelle valeur raccourcies). Rien n'est enregistré au tout premier chargement ; les 2000 derniers changements sont conservés. Pour `full_text`, seul le fait que le texte a changé est noté. Consultable via `get_data_status`.

## Sécurité

- Le token Bearer protège tout sauf `/health`. Il est comparé en temps constant (empreintes SHA-256 et `hmac.compare_digest`).
- **Ne pas exposer le serveur sans TLS** : le token transiterait en clair. Placez un reverse proxy HTTPS (Caddy, Traefik, Nginx...) devant le port 8000 et ne publiez pas le port en clair sur Internet.
- Les données servies sont publiques (fiches du site MYA) ; le token protège surtout contre l'usage abusif de votre instance.
- `/health` ne renvoie que l'état, le nombre de destinations et l'âge des données.
- Le conteneur tourne avec un utilisateur non root. La requête de recherche est assainie : seuls des mots entre guillemets sont transmis à FTS5.
- Pensez à renouveler le token si vous le soupçonnez compromis.

## Sauvegarde du volume

La seule donnée à conserver est `mya.db`, mais elle se reconstruit entièrement par un scrape (seul l'historique des changements serait perdu). Sauvegarde d'un volume nommé :

```bash
docker run --rm -v mya-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/mya-data.tar.gz -C /data .
```

Restauration : même commande avec `tar xzf` vers un volume vide. Le nom réel du volume avec Compose est préfixé par le nom du projet (`docker volume ls`).

## Dépannage

| Symptôme | Piste |
|---|---|
| Le conteneur s'arrête au démarrage avec `MCP_AUTH_TOKEN est obligatoire` | Définir `MCP_AUTH_TOKEN` (ou `ALLOW_NO_AUTH=1` en local). |
| `401 unauthorized` | Token absent ou différent. L'en-tête doit être exactement `Authorization: Bearer <token>`. |
| `/health` renvoie `no_data` | Premier scrape en cours ou en échec ; voir `docker logs`. |
| `/health` renvoie `stale` | Le refresh échoue depuis plus de `MAX_AGE_HOURS` ; consulter `last_error` via `get_data_status` et les logs. L'ancienne base reste servie. |
| `unable to open database file` / erreurs de droits | Le dossier `/data` doit être inscriptible par l'UID 10001. Avec un bind mount : `chown 10001 <dossier>`. Un volume nommé n'a pas ce souci. |
| Le client ne voit pas les outils | Vérifier l'URL (elle doit finir par `/mcp`), le transport `http` (streamable HTTP) et le token. |
| Erreur `Invalid Content-Type` | Les requêtes brutes doivent envoyer `Content-Type: application/json` et `Accept: application/json, text/event-stream`. |

Les logs (`docker logs mya-mcp`) indiquent chaque refresh, ses échecs et la date du prochain.

## Développement local et tests

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q tests                       # tests hors réseau

# lancer le serveur localement (sans Docker)
DATA_DIR=./data ALLOW_NO_AUTH=1 PORT=8000 python -m mya_mcp
```

Les tests couvrent la conversion HTML, la recherche et l'assainissement des requêtes, l'historique des changements, les scénarios d'échec du scrape (ancienne base conservée) et l'authentification. Le dossier `data/` est ignoré par git.

Le SDK `mcp` est épinglé en 1.x (`mcp==1.30.0`) : la branche 2.x a renommé `FastMCP` en `MCPServer` et n'est pas supportée par ce code.

## Limites

- L'API de mya.epitech.eu n'est pas une API publique documentée : son format ou son accès peuvent changer sans préavis, ce qui casserait le scrape (les garde-fous conservent alors les dernières données valides).
- `spots` est le nombre de places **total** de la destination, pas le nombre de places restantes.
- Les données ne sont fraîches qu'au rythme du refresh (quotidien). Les textes des fiches sont en anglais ou en français selon la destination.
- Le nombre de destinations évolue avec le site (138 lors des tests).
- Aucune donnée personnelle ni authentification Epitech : seules les informations publiques du site sont lues.
- Pas de support OAuth ; un seul token partagé.
- Respectez le site source : un scrape par jour avec 8 requêtes simultanées suffit.

## Licence

[MIT](LICENSE).
