# Testing

| Suite | Command | Needs | Count |
| --- | --- | --- | --- |
| Unit, API, acceptance | `uv run pytest -m "not perf"` | nothing | 217 |
| Integration | `RECO_TEST_DSN=… uv run pytest tests/integration -q` | a disposable PostgreSQL | 24 |
| Performance (A13) | `uv run pytest -m perf` | nothing | 1 |
| End to end | `SWIPREAD_API_DIR=../api scripts/e2e.sh` | JDK 25, Docker, a clone of the API, the embedding model | 11 |

Lint: `uv run ruff check` (line length 100).

## Continuous integration

`.github/workflows/ci.yml` runs on every push to `main` and every pull request: `uv sync --locked`, the lint, then every test but perf and e2e, the integration ones against a `pgvector/pgvector:0.8.7-pg17` service (the production image). `scripts/check_integration_ran.py` reads the JUnit report and fails the run if the integration tests were skipped, as they are silently without `RECO_TEST_DSN`. The check "Lint and test (Python 3.12)" is required to merge into `main`.

The performance test is left out because its timing depends on the machine; the end-to-end tests because they need the API and the model.

## Unit, API and acceptance tests

They run without database, Redis or model: the algorithm is made of pure functions, `tests/fakes.py` provides an in-memory `Store` and the Redis `seen:{userId}` sets, and `fakeredis` stands for Redis in the worker tests.

- `tests/unit`: profile, score, allocation, assembly, parameters, worker activities, events stream consumer, stored profiles.
- `tests/api`: the HTTP routes over the in-memory store.
- `tests/acceptance`: the acceptance tests A1 to A12 of the specification (section 11.2), on a synthetic catalogue of 10 topics and 1,800 articles, all deterministic (seed and clock fixed).

## Integration tests

```bash
docker compose up -d
RECO_TEST_DSN=postgresql://postgres:postgres@localhost:5433/postgres uv run pytest tests/integration -q
```

**The database at `RECO_TEST_DSN` is wiped**: point it only at a disposable one. Without the variable these tests are skipped. They recreate the API's schema from `sql/app_schema.sql` (a copy of the API's migrations, to keep in sync with each new one) with that superuser connection, then run the `PgStore` as a role `reco_test` member of `swipread_reco`: they prove that the rights the API grants the recommender are enough.

## Performance test

`uv run pytest -m perf` checks A13: the computation alone (profile, score, allocation, assembly) under 80 ms at the 95th percentile, with 5,000 articles in the catalogue and 300 events for the reader. The result depends on the machine; a loaded laptop can miss it by a few milliseconds.

## End-to-end tests

`scripts/e2e.sh` runs the real API with the recommender and checks scenarios E1 to E11 of `tests/e2e/` through the API's public routes, as the web app uses them: sign-up, choice of topics, feed pages, likes, hides, recommender outage, latency, profile kept up to date by the `events` stream, account deletion.

Requirements: JDK 25, Docker running, uv, a clone of Swipread/api, and the model `intfloat/multilingual-e5-small` in the Hugging Face cache (the worker runs with `HF_HUB_OFFLINE=1`; set `HF_HUB_OFFLINE=0` to download it at the first run, and `HF_HUB_DISABLE_XET=1` if the download hangs).

```bash
SWIPREAD_API_DIR=../api scripts/e2e.sh          # start everything, test, stop everything
SWIPREAD_API_DIR=../api scripts/e2e.sh --keep   # leave it running after the tests
scripts/e2e.sh down                             # stop what --keep left
```

Arguments after `--keep` (or instead of it) go to pytest, for instance `-k e4 -q`. The script:

1. starts the PostgreSQL and Redis of the API's `compose.yaml` under the Compose project `swipread-e2e` (`E2E_COMPOSE_PROJECT` to change it), on ports chosen by Docker;
2. builds the API if its sources changed (`./mvnw -DskipTests package`, in its repository) and starts it without a profile, as in [Getting started](getting-started.md#run-next-to-the-api), on a free port;
3. creates the role `reco_e2e`, member of `swipread_reco`, and writes the test data (`tests/e2e/seed.py`): the 300 sample articles spread over 6 sources, republished over the last 10 days, with synthetic embeddings per topic (geometry of spec section 11.1); a near duplicate of one article; 6 French articles without embedding, for the worker;
4. starts the recommender's worker and api as `reco_e2e`, on a free port, and waits until the api answers and the worker has written the fallback lists;
5. runs `pytest -m e2e`, prints the measures (`measures.txt`), then stops everything and removes the containers.

Each process logs to a temporary folder whose path is printed. A full run takes about 1 min 30, half a minute of it for the worker to load the model; the first one also builds the API and downloads its Maven dependencies.

## Simulated readers report

```bash
uv run python -m swipread_reco.sim.report --out report/
```

Writes `report/report.md` and `report/report.csv`: four simulated readers over 100 cards, measured page by page (spec section 11.3). Options: `--cards N`, `--seed N`. The `report/` folder is ignored by git.
