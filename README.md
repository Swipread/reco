# swipread-reco

Service de recommandation d'articles de Swipread : il compose, pour chaque lecteur, un lot d'articles à partir des thèmes choisis à l'inscription et de ses interactions, avec le détail du score de chaque carte. Un processus `worker` à côté encode les nouveaux articles, tient les listes de repli et purge les vieilles traces.

Spécification : [`docs/algo_reco.md`](docs/algo_reco.md). Contrat avec le backend : [`docs/Contrat backend reco.md`](docs/Contrat%20backend%20reco.md).

## Prérequis

- Python 3.12
- [uv](https://docs.astral.sh/uv/) (`uv sync` installe les dépendances)
- Docker, pour la base de test : `docker compose up -d` lance PostgreSQL + pgvector (port 5433) et Redis (port 6380)

## Tests

```sh
uv run pytest -m "not perf"     # tests unitaires, d'acceptation et de l'API, sans infrastructure
uv run ruff check

# intégration (PostgreSQL réel) : la base pointée est vidée, n'utiliser qu'une base jetable
RECO_TEST_DSN=postgresql://postgres:postgres@localhost:5433/postgres uv run pytest tests/integration -q

# performance (A13), lancé à part
uv run pytest -m perf
```

Sans `RECO_TEST_DSN`, les tests d'intégration sont ignorés.

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
| `RECO_PORT` | `8000` | port de l'api |
| `RECO_DB_TIMEOUT_S` | `2` | attente maximale d'une connexion PostgreSQL, en secondes. Au-dessus de la coupure à 300 ms du backend : à baisser en production |
| `RECO_<PARAM>` | valeurs de la section 10 | surcharge d'un paramètre de `Params` : nom du champ en majuscules, par ex. `RECO_P_MAX=0.5`, `RECO_DUP_SIM=0.85` |

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
