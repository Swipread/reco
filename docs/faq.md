# FAQ and troubleshooting

## Running locally

**`ModuleNotFoundError: swipread_reco`.**
The editable install is not importable in this environment. Prefix the command with `PYTHONPATH=src`.

**The process stops at start with "schema reco is missing" or "type vector is missing".**
The database has not received the API's migrations. Start the API once on it (or run its migrations), then the recommender. See [Getting started](getting-started.md#run-next-to-the-api).

**It stops with "reco.user_profiles is the backend's old table".**
The database has the API's migrations up to V7 only: V8 drops that old table so the recommender can create its own. Update the API and run its migrations.

**It stops with "no read-write rights on reco tables …".**
The `reco` tables belong to a role whose rights the recommender's role does not have, typically after a dump restored with `pg_restore --no-owner`: the tables then belong to the restoring role, and PostgreSQL restores the schema's default privileges only after them. Restore without `--no-owner` once the login role exists (the tables go back to it), or give them back: `ALTER TABLE reco.<table> OWNER TO reco_service` for each table named.

**The api answers `items: []` for everyone.**
The catalogue is empty: no article has an embedding of the current model yet (start the worker and wait for it to encode), or every article is older than 14 days (`MAX_AGE_DAYS`). The API's `dev` sample articles are dated at their migration and age out: the end-to-end seed republishes them. `/healthz` gives the catalogue size.

**404 `unknown user`.**
The `userId` is not in `app.users`: the API sends only existing readers, so check the database the recommender points to.

**503 `store unavailable`.**
A database call failed or timed out (`RECO_DB_TIMEOUT_S`); the log has the traceback after `store call … failed`.

**The worker's first encoding takes long, or hangs.**
It loads `intfloat/multilingual-e5-small` (about 470 MB) from Hugging Face the first time. If the download does not progress, set `HF_HUB_DISABLE_XET=1` to fall back to plain HTTP. Once cached, run with `HF_HUB_OFFLINE=1`.

**uvicorn logs `Unsupported upgrade request`.**
The API's JDK HTTP client offers an HTTP/2 upgrade on each new connection; uvicorn declines and answers in HTTP/1.1. Harmless.

## Tests

**The integration tests are all skipped.**
`RECO_TEST_DSN` is not set. Start `docker compose up -d` and set it to the disposable database on port 5433. In CI, a missing variable fails the run on purpose.

**The performance test fails by a few milliseconds.**
A13 measures time on the machine at hand: a busy laptop can miss 80 ms at the 95th percentile. Run it again on an idle machine; CI does not run it.

**An end-to-end run fails waiting for the fallback lists.**
The worker was still downloading the model when the script gave up after 120 s. Download it once (see above), then run again.

## Understanding a batch

**Why did this card come out?**
`GET /v1/explain/requests/{requestId}` gives each card's closeness, freshness, penalty, topic weight, nearest liked and rejected articles, and whether the source rule was relaxed. The `requestId` travels with every card in the API's events.

**Why does this reader get so much of one topic?**
`GET /v1/explain/users/{userId}` gives the weights, the shares after the 60 % cap, and the last 50 events that changed a weight.

**Is the worker reading the stream?**
`XINFO GROUPS events` in Redis: the group `reco` must exist, with a small `lag` and `pending` at 0 most of the time. `reco.user_profiles.computed_at` shows when each profile was last stored.
