# Configuration

Both processes are configured by environment variables only; there is no configuration file.

## Environment variables

| Variable | Default | Read by | Role |
| --- | --- | --- | --- |
| `RECO_DATABASE_URL` | required | api, worker | PostgreSQL DSN, with a login role member of `swipread_reco` |
| `RECO_REDIS_URL` | required | api, worker | Redis URL: the API's Redis (`seen:{userId}`, `fallback:{slug}`, stream `events`) |
| `RECO_DB_TIMEOUT_S` | `2` | api, worker | Longest wait for a PostgreSQL connection, in seconds. Above the API's 300 ms timeout: lower it in production |
| `RECO_HOST` | `127.0.0.1` (`0.0.0.0` in the image) | api | Address the api listens on; `0.0.0.0` in a container |
| `RECO_PORT` | `8081` | api | Port of the api; the one the API calls by default (`SWIPREAD_RECO_URL=http://localhost:8081`) |
| `RECO_STREAM_CONSUMER` | `worker` | worker | Name of the worker in the consumer group `reco` of the `events` stream. Keep it across restarts so that the worker takes back the messages it read but did not acknowledge. Run one worker at a time |
| `RECO_<PARAM>` | spec section 10 | api, worker | Override of a parameter of the algorithm (below) |
| `HF_HUB_OFFLINE` | unset (`1` in the image) | worker | `1` to load the embedding model from the Hugging Face cache only |
| `HF_HUB_DISABLE_XET` | unset | worker | `1` to download the model over plain HTTP if the default transfer hangs |

## Parameters of the algorithm

Every field of `Params` (`src/swipread_reco/params.py`, the table of [spec section 10](algorithm.md#10-parameters)) can be overridden by `RECO_` followed by its name in upper case: `RECO_P_MAX=0.5`, `RECO_DUP_SIM=0.85`, `RECO_EVENTS_MAX=3000`. The value is converted to the field's type; an invalid one stops the process at start with the variable named.

The parameters are saved with every batch (`reco.feed_requests.params`) and are part of the basis of every stored profile: changing one recomputes the profiles at their next request, without a migration. Give the api and the worker the same values.

Two values depend on the embedding model and are to be checked on real articles: `DUP_SIM` (same story) and `AFF_REASON_MIN` (`reason = similar`).

## Production checklist

- The API's migrations up to V8 have run before the recommender starts.
- A dedicated login role, member of `swipread_reco`, with its own password: `CREATE ROLE reco_service LOGIN PASSWORD '…' IN ROLE swipread_reco`.
- `RECO_HOST=0.0.0.0` in a container, and the api reachable from the API only: its routes are not authenticated.
- `RECO_DB_TIMEOUT_S` lowered under the API's 300 ms timeout (for instance `0.25`).
- One worker, with a stable `RECO_STREAM_CONSUMER`.
- The embedding model available offline (`HF_HUB_OFFLINE=1`), so that a restart never depends on Hugging Face: the Docker image bakes it in (see [Getting started](getting-started.md#docker-image)).
- Memory: the worker holds the model and torch, the api the catalogue (a few MB per thousand articles).
