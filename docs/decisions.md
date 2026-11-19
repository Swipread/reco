# Decisions

The backend/recommender contract and the [specification](algo_reco.md) are the reference; section 15 of the specification says who made each decision of the algorithm. This page records the architecture decisions of this codebase, the known gaps with the backend and what is still open.

## Architecture decision records

### ADR-1 · An explicit model, no learning

Swipread is graded in a demo, without real readers: the feed must visibly adapt to one person and every card must be explainable. The model is a weight per topic and a score formula, with no learned component on data that does not exist. Section 14 of the specification says what would come with real traffic.

### ADR-2 · The algorithm in pure functions

Profile, score, allocation and assembly receive their data and the clock as arguments and never touch the database or Redis. The acceptance tests run on a synthetic catalogue in milliseconds, and two runs on the same state, clock and seed give the same batch.

### ADR-3 · The profile is a replay of the event log

`app.events`, which the API writes before publishing anything, is the source of truth. The profile is recomputed from it, so there is no race between an event and a batch, replaying twice gives the same result, and changing a parameter needs no migration.

### ADR-4 · A stored profile, used only when proven fresh (contract v1.5)

The contract asks the recommender to consume the `events` stream and keep each reader's profile. `reco.user_profiles` keeps the replay with a mark (number of events, last `occurred_at` and `event_id`) and a basis (model, parameters, topics, chosen topics); a request uses it only if both match the moment, else it replays. The count catches an event committed late with an older date, which the contract's mark alone would miss. The age factor and the echo stay computed per request, since they depend on the clock and the previous batch.

### ADR-5 · The stream triggers a replay, not an update

On a message the worker recomputes the reader's profile from `app.events` instead of applying the message. Handling is idempotent and order-insensitive, as at-least-once delivery requires. A failed refresh is acknowledged (the next request recomputes); a failed `user_deleted` stays pending and is retried every minute, with the hourly purge as a safety net. An incremental update would only pay off with histories of thousands of events.

### ADR-6 · Nothing written to `app`

The recommender's role only has `SELECT` on the API's schema. Embeddings go to `reco.article_embeddings` instead of `app.articles.embedding`, without a foreign key (no `REFERENCES` right); orphans are purged hourly.

### ADR-7 · An exact catalogue in memory

A few thousand recent articles fit in memory: similarities are an exact numpy matrix product, and pgvector's HNSW index is not used. The catalogue is reloaded every minute and swapped in whole, so a request never sees half a reload.

### ADR-8 · Recentred similarity

Raw embeddings of any two articles already have a high cosine. Similarity is recentred on the catalogue's mean cosine `mu`, so that closeness, rejection and "same story" thresholds mean the same thing whatever the model's bias.

### ADR-9 · The echo topic, except when the stock already has it

The topic of the latest strong positive signal opens the next batch, so a like visibly changes the feed. When the stock the API kept already holds a card of that topic there is no echo; otherwise several likes in a row would reduce the feed to one topic.

### ADR-10 · One worker, a stable consumer name

The worker reads the stream under a fixed name (`RECO_STREAM_CONSUMER`), so that after a restart it takes back the messages it had read but not acknowledged. Only one worker runs at a time; encoding and the fallback lists need no more.

### ADR-11 · Continuous integration without the timing and end-to-end tests

CI runs the lint, the unit, API, acceptance and integration tests (on the production PostgreSQL image) and fails if the integration tests were skipped. The performance test depends on the machine and the end-to-end tests need the API and the model: they are run apart.

## Gaps with the backend

| Gap | Effect | Status |
| --- | --- | --- |
| The API keeps the first 10 cards of the stock on a strong event, whatever the card's position, instead of the decided n+10 rule | An interaction on card n shows between card n+11 and n+20 | Reported (spec section 13); the simulation still imitates n+10 |
| The web app does not send `scrollDepth` | A complete read is judged by time only (60 % of the expected reading time) | Accepted (spec 4.4) |
| No `cluster_id` on `app.articles` | "Same story" rests on similarity alone | Accepted; the comparison stays in the code for the day the ingestion produces one |

## Open questions

- `DUP_SIM` and `AFF_REASON_MIN` are to be checked on real articles, now that the API ingests real feeds.
- A change of topics publishes no stream message: the basis check covers it at the next request. Whether the API should publish a `topics_changed` message is open in the contract.
- Packaging: container image with a CPU-only torch and the model built in, release workflow, place on the server.
