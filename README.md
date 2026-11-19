# swipread-reco

Service de recommandation d'articles de Swipread : il compose, pour chaque lecteur, un lot d'articles à partir des thèmes choisis à l'inscription et de ses interactions, avec le détail du score de chaque carte. Un processus `worker` à côté encode les nouveaux articles, tient les listes de repli, lit le flux `events` du backend pour tenir à jour le profil de chaque lecteur (et effacer celui d'un compte supprimé), et purge les vieilles traces.

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
| `RECO_STREAM_CONSUMER` | `worker` | nom du worker dans le groupe `reco` du flux `events` ; stable d'un redémarrage à l'autre, pour reprendre les messages lus mais pas acquittés. Un seul worker à la fois |
| `RECO_DB_TIMEOUT_S` | `2` | attente maximale d'une connexion PostgreSQL, en secondes. Au-dessus de la coupure à 300 ms du backend : à baisser en production |
| `RECO_<PARAM>` | valeurs de la section 10 | surcharge d'un paramètre de `Params` : nom du champ en majuscules, par ex. `RECO_P_MAX=0.5`, `RECO_DUP_SIM=0.85` |

## Base de données et rôle restreint

La reco ne crée ni le schéma `reco` ni l'extension `vector` : ils viennent des migrations du backend. Elle se connecte avec un rôle membre du rôle de groupe `swipread_reco` (migration V5 du backend) : lecture seule sur `app`, création de tables et lecture-écriture sur `reco`. Au démarrage, l'api et le worker créent les quatre tables de la reco (`feed_requests`, `feed_items`, `article_embeddings`, `user_profiles`) si elles manquent. La base doit avoir reçu la migration V8 du backend, qui supprime son ancienne table `reco.user_profiles`. Détail : [`docs/database.md`](docs/database.md).

## Lancer à côté du backend

Le profil `dev` du backend inclut le profil `mock-reco` (groupe de profils de son `application.yaml`) : avec `dev` ou `test`, le backend ne contacte jamais la reco et sert des articles tirés au hasard. Il n'appelle la reco (`HttpRecommendationClient`, `POST $SWIPREAD_RECO_URL/v1/recommendations`, coupure à 300 ms) que démarré **sans profil**. Sans profil, il ne lance pas non plus Docker Compose : PostgreSQL, Redis et la clé JWT se passent par variables d'environnement. Recette vérifiée (c'est celle de `scripts/e2e.sh`) :

1. Dans le dépôt du backend, démarrer ses conteneurs et lire les ports que Docker leur a donnés :

   ```sh
   docker compose up -d
   docker compose port postgres 5432    # -> 0.0.0.0:<port postgres>
   docker compose port redis 6379       # -> 0.0.0.0:<port redis>
   ```

2. Construire puis lancer le backend sans profil, avec les données d'exemple de `dev` :

   ```sh
   ./mvnw -DskipTests package
   SPRING_DATASOURCE_URL=jdbc:postgresql://localhost:<port postgres>/swipread \
   SPRING_DATASOURCE_USERNAME=swipread SPRING_DATASOURCE_PASSWORD=swipread \
   SPRING_DATA_REDIS_HOST=localhost SPRING_DATA_REDIS_PORT=<port redis> \
   SPRING_FLYWAY_LOCATIONS=classpath:db/migration,classpath:db/seed,classpath:db/dev-data \
   SWIPREAD_JWT_SECRET=<32 caractères ou plus> \
   SWIPREAD_AUTH_REFRESH_COOKIE_SECURE=false \
   SWIPREAD_RECO_URL=http://localhost:8081 \
   java -jar target/api-0.0.1-SNAPSHOT.jar
   ```

   `SPRING_FLYWAY_LOCATIONS` reprend les migrations de `dev` : 8 thèmes, 300 articles d'exemple, le compte admin. Le journal doit dire `No active profile set` ; un appel à la reco qui échoue y laisse `Recommendation service unavailable`. Vérifié sur une base neuve. La FAQ du backend prévient que, sans profil et sans ces emplacements, Flyway refuse une base qui a reçu les données de `dev` (« Detected applied migration not resolved locally ») ; avec eux, les mêmes migrations sont résolues. Ajouter `SERVER_PORT` si 8080 est pris, `SWIPREAD_CORS_ALLOWED_ORIGINS=http://localhost:5173` pour le front web.
3. Créer le rôle de connexion de la reco, en superutilisateur (dans ce conteneur, `swipread` l'est) :

   ```sql
   CREATE ROLE reco_service LOGIN PASSWORD 'reco' IN ROLE swipread_reco;
   ```

4. Lancer la reco sur ce PostgreSQL et ce Redis, une fois les migrations du backend passées (la reco s'arrête si le schéma `reco` manque) :

   ```sh
   export RECO_DATABASE_URL=postgresql://reco_service:reco@localhost:<port postgres>/swipread
   export RECO_REDIS_URL=redis://localhost:<port redis>/0
   uv run python -m swipread_reco worker   # encode les articles, écrit fallback:{slug}
   uv run python -m swipread_reco api      # écoute sur 127.0.0.1:8081
   ```

Le client HTTP du backend (celui du JDK) propose à chaque nouvelle connexion de passer en HTTP/2 (`Upgrade: h2c`) ; uvicorn refuse et répond en HTTP/1.1, en écrivant `Unsupported upgrade request` dans son journal. C'est sans effet sur les réponses.

## Tests de bout en bout

`scripts/e2e.sh` fait tourner le vrai backend avec la reco et vérifie, par l'API publique du backend comme le front web, les scénarios E1 à E11 de `tests/e2e/` (inscription, choix des thèmes, pages du fil, likes, masquages, panne de la reco, latence, profil tenu à jour par le flux `events`, compte supprimé).

Prérequis : JDK 25, Docker démarré, uv, une copie du dépôt Swipread/api, le modèle `intfloat/multilingual-e5-small` dans le cache Hugging Face (le worker tourne avec `HF_HUB_OFFLINE=1` ; mettre `HF_HUB_OFFLINE=0` pour le télécharger au premier lancement).

```sh
SWIPREAD_API_DIR=../swipread-api scripts/e2e.sh          # tout lancer, tester, tout arrêter
SWIPREAD_API_DIR=../swipread-api scripts/e2e.sh --keep   # laisser tourner après les tests
scripts/e2e.sh down                                      # arrêter ce que --keep a laissé
```

Les arguments après `--keep` (ou à la place) vont à pytest, par exemple `-k e4 -q`. Le script :

1. lance le PostgreSQL et le Redis du `compose.yaml` du backend sous le projet Compose `swipread-e2e` (`E2E_COMPOSE_PROJECT` pour en changer), sur des ports choisis par Docker ;
2. construit le backend s'il a changé (`./mvnw -DskipTests package`, dans son dépôt) et le lance sans profil, comme ci-dessus, sur un port libre ;
3. crée le rôle `reco_e2e`, membre de `swipread_reco`, et écrit les données de test (`tests/e2e/seed.py`) : les 300 articles d'exemple répartis sur 6 sources, republiés sur les 10 derniers jours, avec des embeddings synthétiques par thème (géométrie de la section 11.1 de la spec) ; un quasi-doublon d'un article ; 6 articles en français sans embedding, pour le worker ;
4. lance le worker et l'api de la reco sous le rôle `reco_e2e`, sur un port libre, attend que l'api réponde et que le worker ait écrit les listes de repli ;
5. lance `pytest -m e2e`, affiche les mesures (`measures.txt`), puis arrête tout et supprime les conteneurs.

Les journaux de chaque processus vont dans un dossier temporaire dont le chemin est affiché. Un lancement complet prend environ 1 min 30 (dont une trentaine de secondes de chargement du modèle par le worker) ; le premier construit le backend et télécharge ses dépendances Maven, compter quelques minutes de plus. Sans les variables `E2E_*` que le script exporte, `uv run pytest` ignore ces tests.

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
