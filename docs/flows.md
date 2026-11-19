# Flows

The main sequences between the API and the recommender. The details of each rule are in the [algorithm](algorithm.md).

## A batch for a reader

```mermaid
sequenceDiagram
    autonumber
    participant A as API
    participant R as Recommender api
    participant DB as PostgreSQL
    participant Rd as Redis

    A->>R: POST /v1/recommendations {userId, count, exclude}
    R->>DB: app.users.locale (404 if unknown)
    R->>Rd: SMEMBERS seen:{userId}
    R->>DB: user_articles, topics, chosen topics, last batch time
    R->>DB: reco.user_profiles + mark of app.events
    alt stored profile fresh
        Note over R: use the stored replay
    else stale or missing
        R->>DB: mark + events in one snapshot, replay
        R->>DB: upsert reco.user_profiles
    end
    R->>DB: events received since the last batch (echo)
    Note over R: finish the profile, score, allocate, fill
    R->>DB: INSERT feed_requests + feed_items
    R-->>A: requestId, modelVersion, items [{articleId, score, reason}]
```

The API keeps the batch in the reader's stock and serves it page by page; the recommender only sees the next call, with the cards still in stock as `exclude`.

## A strong signal

```mermaid
sequenceDiagram
    autonumber
    participant C as Web app
    participant A as API
    participant DB as PostgreSQL
    participant Rd as Redis
    participant W as Recommender worker
    participant R as Recommender api

    C->>A: POST /v1/events [like on a_1]
    A->>DB: INSERT app.events + outbox (one transaction)
    A->>A: strong event: trim the stock to 10, refill in the background
    A->>R: POST /v1/recommendations
    R->>DB: mark of app.events is ahead of the stored profile
    R->>DB: replay, store the profile
    R-->>A: batch starting with the topic of a_1 (echo)
    A->>Rd: XADD events (outbox relay, ≤ 200 ms)
    W->>Rd: XREADGROUP reco
    W->>DB: replay from app.events, profile already up to date
    W->>Rd: XACK
```

The refill often arrives before the stream message: the mark check makes the request replay, so the like is in the batch either way. The worker's later refresh gives the same profile.

## A weak signal

A `view`, `open` or non-complete `read` does not make the API refill. The worker reads its message and stores the new profile; the next batch, whenever it comes, finds it fresh and skips the replay.

## A new article

```mermaid
sequenceDiagram
    autonumber
    participant I as API ingestion
    participant DB as PostgreSQL
    participant W as Recommender worker
    participant R as Recommender api

    I->>DB: INSERT app.articles + app.article_topics
    W->>DB: articles without an embedding of the current model (every 15 s, 64 at most)
    W->>W: encode "query: title. summary" (multilingual-e5-small)
    W->>DB: upsert reco.article_embeddings
    R->>DB: catalogue reload (every 60 s)
    Note over R: the article can be recommended
```

An article is recommendable at most about 75 seconds after its ingestion, encoding time aside, while it is less than 14 days old (`MAX_AGE_DAYS`) and its source is active. Every 10 minutes the worker also rewrites the fallback lists `fallback:{slug}` the API serves when the recommender is down.

## A deleted account

```mermaid
sequenceDiagram
    autonumber
    participant A as API
    participant Rd as Redis
    participant W as Recommender worker
    participant DB as PostgreSQL

    A->>DB: DELETE the account and its events
    A->>Rd: XADD events {type: user_deleted}
    W->>Rd: XREADGROUP reco
    W->>DB: DELETE reco.user_profiles, reco.feed_requests (items by cascade)
    W->>Rd: XACK
```

If the deletion fails, the message stays pending and the worker takes it again every minute. The hourly purge also deletes the batches and profiles of any account gone from `app.users`.
