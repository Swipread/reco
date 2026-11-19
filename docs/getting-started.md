# Getting started

Install the recommender, run its tests, then run it next to the real API.

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/): `uv sync` installs the dependencies, development tools included
- Docker: `docker compose up -d` starts PostgreSQL 17 + pgvector (port 5433) and Redis 7.4 (port 6380), the API's images
- To run next to the API: JDK 25 and a clone of [Swipread/api](https://github.com/Swipread/api)

```bash
git clone https://github.com/Swipread/reco.git
cd reco
uv sync
uv run pytest -m "not perf"
```

The tests and their variants are described in [Testing](testing.md).

## Run the api and the worker

```bash
export RECO_DATABASE_URL=postgresql://reco_service:reco@localhost:<postgres port>/swipread
export RECO_REDIS_URL=redis://localhost:<redis port>/0

uv run python -m swipread_reco worker
uv run python -m swipread_reco api
```

Both stop at start with an explicit message if the database lacks what the API's migrations create (schema `reco`, type `vector`) or still has the API's old `reco.user_profiles` table (dropped by its migration V8): run the API's migrations first. The database to use is the API's; see [Run next to the API](#run-next-to-the-api).

If the package installed in editable mode cannot be imported (`ModuleNotFoundError: swipread_reco`), prefix the commands with `PYTHONPATH=src`.

The worker loads the embedding model `intfloat/multilingual-e5-small` (about 470 MB) at its first encoding, from the Hugging Face cache or by downloading it. If the download hangs, set `HF_HUB_DISABLE_XET=1` to fall back to plain HTTP.

## Environment variables

`RECO_DATABASE_URL` and `RECO_REDIS_URL` are required; the others (`RECO_HOST`, `RECO_PORT`, `RECO_STREAM_CONSUMER`, `RECO_DB_TIMEOUT_S`, the `RECO_<PARAM>` overrides of the algorithm) have defaults. All of them: [Configuration](configuration.md).

## Database and restricted role

The recommender creates neither the `reco` schema nor the `vector` extension: they come from the API's migrations. It connects with a login role member of the group role `swipread_reco` (API migration V5): read only on `app`, table creation and read-write on `reco`. At start, the api and the worker create the recommender's four tables (`feed_requests`, `feed_items`, `article_embeddings`, `user_profiles`) if they are missing. Details: [Database](database.md).

## Run next to the API

The API's `dev` profile includes its `mock-reco` profile: with `dev` or `test`, the API never calls the recommender and serves random articles. It calls it (`POST $SWIPREAD_RECO_URL/v1/recommendations`, 300 ms timeout) only when started **without a profile**. Without a profile it does not start Docker Compose either: PostgreSQL, Redis and the JWT secret come from environment variables. The verified recipe, the one `scripts/e2e.sh` follows:

1. In the API's repository, start its containers and read the ports Docker gave them:

   ```bash
   docker compose up -d
   docker compose port postgres 5432    # -> 0.0.0.0:<postgres port>
   docker compose port redis 6379       # -> 0.0.0.0:<redis port>
   ```

2. Build the API, then start it without a profile, with the `dev` sample data:

   ```bash
   ./mvnw -DskipTests package
   SPRING_DATASOURCE_URL=jdbc:postgresql://localhost:<postgres port>/swipread \
   SPRING_DATASOURCE_USERNAME=swipread SPRING_DATASOURCE_PASSWORD=swipread \
   SPRING_DATA_REDIS_HOST=localhost SPRING_DATA_REDIS_PORT=<redis port> \
   SPRING_FLYWAY_LOCATIONS=classpath:db/migration,classpath:db/seed,classpath:db/dev-data \
   SWIPREAD_JWT_SECRET=<32 characters or more> \
   SWIPREAD_AUTH_REFRESH_COOKIE_SECURE=false \
   SWIPREAD_RECO_URL=http://localhost:8081 \
   java -jar target/api-*.jar
   ```

   `SPRING_FLYWAY_LOCATIONS` adds the `dev` migrations: 8 topics, 300 sample articles, the admin account. The log must say `No active profile set`; a failed call to the recommender leaves `Recommendation service unavailable` in it. Without these locations, Flyway refuses a database that received the `dev` data ("Detected applied migration not resolved locally"), as the API's FAQ explains. Add `SERVER_PORT` if 8080 is taken, `SWIPREAD_CORS_ALLOWED_ORIGINS=http://localhost:5173` for the web app.

3. Create the recommender's login role, as a superuser (in this container, `swipread` is one):

   ```sql
   CREATE ROLE reco_service LOGIN PASSWORD 'reco' IN ROLE swipread_reco;
   ```

4. Start the recommender on this PostgreSQL and Redis, once the API's migrations have run:

   ```bash
   export RECO_DATABASE_URL=postgresql://reco_service:reco@localhost:<postgres port>/swipread
   export RECO_REDIS_URL=redis://localhost:<redis port>/0
   uv run python -m swipread_reco worker   # encodes the articles, writes fallback:{slug}, reads the stream
   uv run python -m swipread_reco api      # listens on 127.0.0.1:8081
   ```

The API's HTTP client (the JDK's) offers an upgrade to HTTP/2 (`Upgrade: h2c`) on each new connection; uvicorn declines, answers in HTTP/1.1 and logs `Unsupported upgrade request`. The responses are not affected.

## Docker image

```bash
docker build -t swipread-reco .
docker run --rm -p 8081:8081 -e RECO_DATABASE_URL=… -e RECO_REDIS_URL=… swipread-reco          # api
docker run --rm -e RECO_DATABASE_URL=… -e RECO_REDIS_URL=… swipread-reco worker                # worker
```

The image (CPU-only torch, embedding model included, about 2 GB) and its use in production are described in [Deployment](deployment.md).
