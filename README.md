# swipread-reco

Service de recommandation d'articles de Swipread : il compose, pour chaque lecteur, un lot d'articles à partir des thèmes choisis à l'inscription et de ses interactions, avec le détail du score de chaque carte. Un processus `worker` à côté encode les nouveaux articles, tient les listes de repli et purge les vieilles traces.

Spécification : [`docs/algo_reco.md`](docs/algo_reco.md). Contrat avec le backend : [`docs/Contrat backend reco.md`](docs/Contrat%20backend%20reco.md).

## Prérequis

- Python 3.12
- [uv](https://docs.astral.sh/uv/) (`uv sync` installe les dépendances)
- Docker, pour la base de test : `docker compose up -d` lance PostgreSQL 17 + pgvector (port 5433) et Redis 7.4 (port 6380), les mêmes images que le backend

## Tests

```sh
uv run pytest -m "not perf"     # tests unitaires, d'acceptation et de l'API, sans infrastructure
uv run ruff check

# intégration (PostgreSQL réel) : la base pointée est vidée, n'utiliser qu'une base jetable
RECO_TEST_DSN=postgresql://postgres:postgres@localhost:5433/postgres uv run pytest tests/integration -q

# performance (A13), lancé à part
uv run pytest -m perf
```

Sans `RECO_TEST_DSN`, les tests d'intégration sont ignorés. Ils recréent le schéma du backend depuis `sql/app_schema.sql` (copie des migrations de Swipread/api) avec la connexion superutilisateur de `RECO_TEST_DSN`, puis font tourner le `PgStore` sous un rôle `reco_test` membre de `swipread_reco` : ils vérifient ainsi que les droits donnés à la reco par le backend suffisent.

## Lancer l'API et le worker

```sh
export RECO_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/postgres
export RECO_REDIS_URL=redis://localhost:6380/0

uv run python -m swipread_reco api
uv run python -m swipread_reco worker
```

Si le paquet installé en mode éditable n'est pas importable (`ModuleNotFoundError: swipread_reco`), préfixer par `PYTHONPATH=src` :

```sh
PYTHONPATH=src uv run python -m swipread_reco api
```

## Variables d'environnement

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `RECO_DATABASE_URL` | obligatoire | DSN PostgreSQL (api et worker) |
| `RECO_REDIS_URL` | obligatoire | URL Redis (api et worker) |
| `RECO_HOST` | `127.0.0.1` | adresse d'écoute de l'api ; mettre `0.0.0.0` dans un conteneur |
| `RECO_PORT` | `8081` | port de l'api ; celui qu'appelle le backend par défaut (`SWIPREAD_RECO_URL=http://localhost:8081`) |
| `RECO_DB_TIMEOUT_S` | `2` | attente maximale d'une connexion PostgreSQL, en secondes. Au-dessus de la coupure à 300 ms du backend : à baisser en production |
| `RECO_<PARAM>` | valeurs de la section 10 | surcharge d'un paramètre de `Params` : nom du champ en majuscules, par ex. `RECO_P_MAX=0.5`, `RECO_DUP_SIM=0.85` |

## Base de données et rôle restreint

La reco ne crée ni le schéma `reco` ni l'extension `vector` : ils viennent des migrations du backend. Elle se connecte avec un rôle membre du rôle de groupe `swipread_reco` (migration V5 du backend) : lecture seule sur `app`, création de tables et lecture-écriture sur `reco`. Au démarrage, l'api et le worker créent les trois tables de la reco (`feed_requests`, `feed_items`, `article_embeddings`) si elles manquent. Détail : [`docs/database.md`](docs/database.md).

## Lancer à côté du backend

1. Démarrer le backend (Swipread/api) une première fois, pour que Flyway crée la base. Son `compose.yaml` publie PostgreSQL et Redis sur des ports choisis par Docker : `docker compose port postgres 5432` et `docker compose port redis 6379`, dans le dépôt du backend, les donnent.
2. Créer le rôle de connexion de la reco, en tant que propriétaire de la base (en dev, l'utilisateur `swipread`) :

   ```sql
   CREATE ROLE reco_service LOGIN PASSWORD 'reco' IN ROLE swipread_reco;
   ```

3. Lancer la reco sur ce PostgreSQL et ce Redis :

   ```sh
   export RECO_DATABASE_URL=postgresql://reco_service:reco@localhost:<port postgres>/swipread
   export RECO_REDIS_URL=redis://localhost:<port redis>/0
   uv run python -m swipread_reco worker   # encode les articles, écrit fallback:{slug}
   uv run python -m swipread_reco api      # écoute sur 127.0.0.1:8081
   ```

Le profil `dev` du backend inclut le profil `mock-reco` (groupe de profils de son `application.yaml`) : avec `dev` ou `test`, le backend ne contacte jamais la reco et sert des articles tirés au hasard. Il n'appelle la reco (`HttpRecommendationClient`, `POST $SWIPREAD_RECO_URL/v1/recommendations`, coupure à 300 ms) que démarré sans profil, avec les variables de sa « Production checklist » (`docs/configuration.md` du backend) : `SPRING_DATASOURCE_URL`, `SPRING_DATASOURCE_USERNAME`, `SPRING_DATASOURCE_PASSWORD`, `SPRING_DATA_REDIS_HOST`, `SPRING_DATA_REDIS_PORT`, `SWIPREAD_JWT_SECRET`, `SWIPREAD_CORS_ALLOWED_ORIGINS`, et `SWIPREAD_RECO_URL` si la reco n'est pas sur `http://localhost:8081`. Sa FAQ prévient que sans profil, Flyway refuse une base qui a reçu les données de `dev` : garder les mêmes migrations que `dev` avec `SPRING_FLYWAY_LOCATIONS=classpath:db/migration,classpath:db/seed,classpath:db/dev-data`, et `SWIPREAD_AUTH_REFRESH_COOKIE_SECURE=false` en HTTP. Cette combinaison, déduite du code du backend, n'a pas été essayée.

## Routes

Les routes ne sont pas authentifiées : l'api ne doit être joignable que depuis le réseau interne.

- `POST /v1/recommendations` : lot d'articles pour un lecteur (contrat backend)
- `GET /healthz` : état et taille du catalogue en mémoire
- `GET /v1/explain/users/{userId}` : poids par thème, articles aimés et rejetés, historique des changements de poids
- `GET /v1/explain/requests/{requestId}` : lot enregistré, avec le détail du score de chaque carte

## Rapport pour le jury

```sh
PYTHONPATH=src uv run python -m swipread_reco.sim.report --out report/
```

Écrit `report/report.md` et `report/report.csv` : quatre lecteurs simulés sur 100 cartes, mesurés page par page (spec 11.3). Options : `--cards N`, `--seed N`.
