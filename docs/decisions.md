# Decisions

The backend/recommender contract and the [algorithm](algorithm.md) are the reference. This page records the architecture decisions of this codebase, the known gaps with the backend, what real traffic would change and what is still open. The algorithm's goals and its main choices were set by the recommendation team; the rest was proposed during the design and validated by that team.

## Architecture decision records

### ADR-1 · An explicit model, no learning

Swipread is graded in a demo, without real readers: the feed must visibly adapt to one person and every card must be explainable. The model is a weight per topic and a score formula, with no learned component on data that does not exist. [With real traffic](#with-real-traffic) says what would come with it.

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
| On a strong event (`event/StrongEventPolicy.java`), `FeedRefiller.invalidate` keeps the first 10 cards of the stock whatever the position of the card, then refills; the n+10 rule decided with the API kept the first `p` | With the cards still on the phone (9 − p) and the 10 kept, an interaction on card n shows between card n+11 and n+20, not at n+10 | Reported; the simulated readers (spec 11.3) still imitate n+10 |
| The web app does not send `scrollDepth` | A complete read is judged by time only (60 % of the expected reading time) | Accepted (spec 4.4) |
| No `cluster_id` on `app.articles` | "Same story" rests on similarity alone | Accepted; the comparison stays in the code for the day the ingestion produces one |

## With real traffic

What v1 leaves out, and what would trigger adding it. The data needed is already saved: every card served, its position, its detailed score and the version of the algorithm.

| Topic | What we would do | When |
| --- | --- | --- |
| Learned ranking | Train a model (LightGBM) to predict opening from the terms of the score, on `reco.feed_items` joined with `app.events`, correcting the position bias | A few tens of thousands of cards served |
| Popularity | Add a "read by many people" term and a `trending` reason | As soon as there are several hundred active readers |
| Collaborative filtering | Recommend what readers with close tastes liked (`reason = collaborative`) | Same |
| A/B test | Serve two `modelVersion` to two groups and compare the open rates | As soon as a change of algorithm competes with the existing one |
| Incremental profile | Apply each message of the `events` stream to the stored profile instead of replaying the history (ADR-5) | When a reader's history goes beyond a few thousand events |
| Approximate search | Use pgvector's HNSW index instead of the exact in-memory computation (ADR-7) | Catalogue beyond about 100,000 articles |
| Grouped interests | Sum up the liked articles in a few centres instead of keeping them all | When the liked lists go beyond a few hundred |

## Open questions

- `DUP_SIM` and `AFF_REASON_MIN` are to be checked on real articles, now that the API ingests real feeds.
- A change of topics publishes no stream message: the basis check covers it at the next request. Whether the API should publish a `topics_changed` message is open in the contract.
- Packaging: container image with a CPU-only torch and the model built in, release workflow, place on the server.
