# Algorithm

The recommendation algorithm: how a reader's profile is built, how articles are scored, how a batch is allocated between topics and filled, with its parameters and acceptance tests. It is the reference for any change of behaviour; the code cites its sections ("spec 4.2"), whose numbers are stable. How the service runs around it is in [Architecture](architecture.md), [Flows](flows.md) and [Database](database.md); the exchange formats are those of the [API reference of Swipread/api](https://github.com/Swipread/api/blob/main/docs/api.md).

## 1. Intent

Swipread is shown in a demo to a jury, without real readers. The jury grades the product and the technical quality of the algorithm. Two things must therefore be true:

- the feed visibly adapts to the interactions of a single person, live;
- we can explain why each card came out, figures included.

Hence the choice: a fully explicit model (weights per topic and a score formula), with no learning on volumes of data that do not exist.

### Goals

- The topics chosen at sign-up form the base of the feed; interactions make it evolve.
- A strong interaction on card n changes the feed from card n+10 on (stock rule decided with the backend; the current backend gives between n+11 and n+20), and the change is clear: the first recomputed card belongs to the topic just liked, unless a card of that topic already waits in the stock the backend kept (4.4).
- An unchosen topic that the reader hides without ever having liked it leaves the feed.
- Every card served is saved with the breakdown of its score.
- Quality is measured offline with simulated readers (section 11), which are also the acceptance tests.

### Out of scope

- A learned ranking model (LightGBM or other), popularity statistics, collaborative filtering, A/B tests, a separate session vector.
- Consent to personalisation (`app.users.personalization_consent_at`): it does not exist in the API, the service ignores it.
- The screen that shows the explanations to the jury. The service exposes the data (section 9); how to display it is to be decided.

### What would be a failure even if everything runs

- A card nobody can say why it came out.
- A politics article served right after a "not interested" on politics, an unchosen topic.
- A feed that shrinks to a single topic after a few likes.
- Two identical runs (same database, same clock, same seed) that give two different batches.

## 2. Overview

```mermaid
flowchart LR
    EVT["app.events<br/>of the reader"] --> PROF["Profile<br/>weight per topic<br/>liked / rejected articles"]
    UT["app.user_topics"] --> PROF
    CAT["In-memory catalogue<br/>recent articles + embeddings"] --> SCORE["Score per article<br/>closeness, freshness, rejection"]
    PROF --> SCORE
    PROF --> ALLOC["Allocation of the places<br/>between topics"]
    SCORE --> LOT["Assembly of the batch<br/>no duplicate, varied sources"]
    ALLOC --> LOT
    LOT --> REP["Response<br/>ids + score + reason"]
    LOT --> LOG["reco.feed_requests<br/>reco.feed_items"]
```

At each `POST /v1/recommendations` call, the service:

1. takes the reader's profile stored in `reco.user_profiles` if it is up to date, or rebuilds it by replaying their events from `app.events` (section 4, 8.6);
2. scores each candidate article (section 5);
3. allocates the places of the batch between topics according to the weights (section 6);
4. fills each place with the best article of the topic (section 7);
5. saves the batch and its explanation, then answers.

The profile remains a pure function of the history: the one `reco.user_profiles` keeps (contract v1.5) is only a copy, used only if it was computed on the same events, the same topics and the same parameters (8.6). Intended consequences: no race between the arrival of an event and the recomputation, replaying twice gives the same result, and changing a parameter recomputes every profile without a migration.

## 3. Vocabulary and input data

| Term | Definition |
| --- | --- |
| Topic | A row of `app.topics`. The list is finite; K is their number. |
| Chosen topic | A topic in `app.user_topics` for the reader, at the time of the request. |
| Main topic of an article | In `app.article_topics`, the topic of highest `confidence`; on a tie, the lowest `topic_id`. An article without a topic is never recommended. |
| Embedding | A vector of 384 numbers, of norm 1, that sums up the meaning of an article (section 8). |
| Batch | The list of articles returned by a call to `/v1/recommendations`. |
| Catalogue | The recommendable articles, kept in memory by the service (section 8). |

The `app` schema is the one the backend's migrations create (Swipread/api); [database.md](database.md) lists what the recommender reads from it and defines the four `reco` tables that belong to the service. The service reads `app` read-only, without exception, and writes to `reco`: exactly the rights of the `swipread_reco` role created by the backend ([Database](database.md#rights)).

The service depends on the ingestion for two things: articles in `app.articles` and at least one topic per article in `app.article_topics`. The backend has no article status: every article of an active source counts as active. It has no `cluster_id` either: the "same story" rule rests on similarity alone (7.2). Development relies on the synthetic catalogue of section 11 and on the backend's sample articles.

All numeric parameters are gathered in section 10. In the text they appear by name (`V_LIKE`, `EPSILON`…).

## 4. Reader profile

The profile holds three things: a weight per topic, the list of liked articles and the list of rejected articles.

### 4.1 Value of an article for the reader

For each (reader, article) pair, the events of type `view`, `open`, `read`, `like`, `unlike`, `save`, `unsave`, `share`, `hide` update a state:

| Event | Effect on the state |
| --- | --- |
| `view` with `dwellMs < T_SKIP` | `skipped = true` |
| `view` with `dwellMs ≥ T_LONG` | `long_view = true` |
| `view` in between | none |
| `open` | `opened = true` |
| `read` | `read = max(read, c)` with `c = min(1, readMs / expected_time)` and `expected_time = word_count / 230 × 60,000` ms. If `word_count` is zero or missing, `expected_time = 60,000`. It is missing too when the backend could only count the feed's title and summary (`app.articles.word_count_from_summary`, contract v1.7): such a count says nothing of the article's length. `scrollDepth` does not enter the value and may be missing: the web app does not measure it. |
| `like` / `unlike` | `liked = true` / `liked = false` |
| `save` / `unsave` | `saved = true` / `saved = false` |
| `share` | `shared = true` |
| `hide` | `hidden = true` (final) |

`impression` is ignored. `word_count` comes from `app.articles`, and is unknown when `word_count_from_summary` is true.

The value `v` of the article follows from the state:

```
if hidden:                          v = V_HIDE            (−0.15)
else:
    pos = max( V_LONG_VIEW if long_view,                 (0.02)
               V_OPEN if opened,                         (0.03)
               V_OPEN + V_READ_BONUS × read if read,     (0.03 to 0.07)
               V_LIKE if liked, saved or shared )        (0.08)
    if pos > 0:                     v = pos
    else if skipped:                v = V_SKIP            (−0.02)
    else:                           v = 0
```

An article therefore counts once, at the level of its strongest signal. A long view, an open, a read then a like on the same article are worth 0.08, not the sum.

### 4.2 Weight per topic

**Start.** With c topics chosen among K:

- each chosen topic gets `(1 − EPSILON) / c`;
- each unchosen topic gets `EPSILON / (K − c)`;
- if c = 0 or c = K, all get `1 / K`.

The weights sum to 1: a weight reads directly as a share of the feed.

**Update.** The reader's events are replayed from the oldest to the most recent (sorted by `occurred_at`, then `event_id`). At most the `EVENTS_MAX` most recent are replayed. For each event, the value of the article is computed before (`v_before`) and after (`v_after`); if `δ = v_after − v_before` is not zero, it is applied to the main topic `t` of the article:

```
if δ > 0:
    w[t] = w[t] + δ
else:
    floor = 0 if (type = hide and t not chosen) else W_FLOOR
    w[t] = max( w[t] + δ, min(w[t], floor) )
w = w / sum(w)            # renormalisation after each update
```

Related rules:

- An event whose article is unknown or has no main topic does not change the weights.
- If the sum drops to 0, the weights start again from the starting weights.
- Only a hide on an unchosen topic can bring a weight to 0, and it does so as soon as the weight is at most 0.15: that is the case of any unchosen topic the reader has not clearly liked before. An unchosen topic raised to 20 % by likes goes down to 5 % at the first hide, then to 0 at the second.
- A topic at 0 gets no place in the feed any more. It comes back if the reader adds it to their chosen topics, or interacts positively with an article of that topic met elsewhere than in the feed (fallback feed, saved list).
- A chosen topic never goes below `W_FLOOR`: the reader asked for it explicitly.
- Renormalising at each step dilutes old gains: recent interactions weigh more than old ones, and the responsiveness stays the same whatever the age of the account.
- If the reader changes their topics (`PUT /v1/me/topics`), nothing special: the next computation starts from the new chosen topics and replays the whole history.

**Worked example** (K = 10, chosen topics: tech, economy, culture; likes without any other signal):

| Step | tech, economy, culture (each) | sport | politics | 5 others (each) |
| --- | --- | --- | --- | --- |
| Start | 30.0 % | 1.4 % | 1.4 % | 1.4 % |
| Like on a sport article | 27.8 % | 8.7 % | 1.3 % | 1.3 % |
| Like on another sport article | 25.7 % | 15.5 % | 1.2 % | 1.2 % |
| Hide on a politics article | 26.0 % | 15.7 % | 0 % | 1.2 % |

Over 10 cards, that gives on average 7.8 cards of the chosen topics, 1.6 of sport, 0.6 of exploration and none of politics.

### 4.3 Liked and rejected articles

After the replay:

- **liked**: articles with `v > 0`, of strength `s = min(1, v / V_LIKE)`;
- **rejected**: articles with `v < 0`, of strength `s = 1` if hidden, `S_SKIP` if only skipped.

Each strength is multiplied by an age factor `0.5 ^ (age_days / HALF_LIFE_PROFILE_DAYS)`, the age counted from the last event on the article. At most `PROFILE_MAX_ITEMS` articles are kept per list, the most recent. Their embeddings are read from `reco.article_embeddings` (8.4), including for articles that left the catalogue; an article without an embedding of the current model, or gone from `app.articles`, is dropped from the list.

### 4.4 Echo topic

The **echo topic** is the main topic of the article of the most recent `like`, `save`, `share` or complete `read` event, among the events whose `received_at` is later than the reader's previous recommendation request (`max(generated_at)` in `reco.feed_requests`). If there is no such event, or if its topic has a zero weight, there is no echo. Section 6 uses it to put that topic first in the batch. `unlike`, `unsave` and `hide` never give an echo.

A `read` is complete exactly when the backend judges it strong (`StrongEventPolicy.java`): `scrollDepth > 0.8`, or `readMs`, truncated to an integer, at least 60 % of `word_count / 230 × 60,000` ms. If `word_count` is zero or unknown (counted on the summary only included, as in the backend), only the first condition counts; a missing `readMs` counts as 0. The web app does not send `scrollDepth`, so in practice the second condition applies. The list is thus exactly the backend's strong positive events, those that trigger the recomputation of the stock: every recomputation caused by a positive signal has its echo. This is the only use of `scrollDepth`; the value of a read is still measured by time (4.1).

Exception: there is no echo if the stock the backend kept (the request's `exclude` list, rule 3 of the contract) already holds an article of that topic. After several strong signals in a row, every card served is the first of a new batch; without this exception the feed would shrink to the echo topic.

The eligibility of an event compares its `received_at`, set by the backend, with the `generated_at` of the previous request, set by the recommender: a clock offset between the two can lose an echo.

## 5. Score of an article

### 5.1 Similarity between two articles

The raw embeddings of any two articles already have a high cosine. They are therefore recentred:

```
sim(a, b) = clamp( (cos(a, b) − MU) / (1 − MU), 0, 1 )
```

`MU` is the mean cosine between two articles drawn at random from the catalogue. It is recomputed at each reload of the catalogue, on 2,000 pairs drawn with a fixed seed (0). If the catalogue has fewer than 50 articles, `MU = 0`.

### 5.2 The three terms

For a candidate `a`:

- **Closeness**: compute `s_j × sim(a, j)` for each liked article `j` and take the mean of the `min(3, number of liked)` largest values. Without a liked article, closeness is 0.
- **Rejection**: the maximum of `s_j × sim(a, j)` over the rejected articles `j`. Without a rejected article, 0.
- **Freshness**: `0.5 ^ (age_hours / HALF_LIFE_FRESH_HOURS)`, the age counted from `published_at` and floored at 0.

Closeness is computed over all liked articles, all topics together: liking an article about AI also brings closer an economy article about AI.

### 5.3 Score

```
raw(a)   = W_AFF × closeness + W_FRESH × freshness − W_NEG × rejection
score(a) = clamp( raw(a), 0, 1 )
```

Ranking uses `raw`, which can be negative: clamping before ranking would tie all penalised articles. `score` is the value returned in the response and saved.

Ranking: `raw` descending, then `published_at` descending, then `id` ascending. As long as the reader has liked nothing, freshness alone ranks the articles.

## 6. Allocation of the places between topics

### 6.1 Shares

Start from the weights `w`. No topic exceeds `P_MAX` of the batch: if a weight exceeds it, bring it back to `P_MAX` and spread the excess over the other topics in proportion to their weights, repeating until none exceeds. If all the others are at 0, do not cap. `p[t]` are the resulting shares. The cap does not change the weights, only the allocation.

### 6.2 Order of the topics in the batch

The proportions must hold on each page of 10 cards, not only on the whole batch. The topics are therefore interleaved this way:

- each topic `t` of non-zero share gets an offset `u[t]` drawn uniformly in [0, 1); the echo topic gets `u = 0`;
- the k-th card of topic `t` (k = 0, 1, 2…) has the virtual rank `τ(t, k) = (k + u[t]) / p[t]`;
- places are given in increasing order of `τ`; on a tie, lowest `topic_id` first.

The stock the backend kept (`exclude`, 4.4) will be served right before the batch: its cards count as already placed. Each topic therefore starts at `k = number of cards of that topic in the stock` instead of 0, and the interleaving goes on instead of starting over. Without this rule, after several strong signals in a row, the reader would only see beginnings of batches and a page could hold 7 to 9 cards of one topic. Only the relative order of the `τ` matters: nothing else changes.

A topic at 30 % thus comes back every 3 or 4 cards. A topic at 1.4 % has a 14 % chance of appearing on a given page. The random offset keeps small topics from always being pushed to the end of the batch, where the batch is most often thrown away before being served.

The draws use `numpy.random.default_rng(seed)`, in increasing order of `topic_id`. The seed is the integer formed by the first 8 bytes of the SHA-256 of the `requestId`, shifted right by one bit to fit in a `bigint`; tests can impose it.

## 7. Assembly of the batch

### 7.1 Candidates

An article of the catalogue is a candidate if it meets all these conditions:

- its language is the reader's: the first two letters of `context.locale`, or failing that of `app.users.locale`;
- it is not in `exclude` (request);
- it is not in the Redis set `seen:{userId}`;
- the reader has no `app.user_articles` row for it, and no replayed event is about it.

If reading `seen:{userId}` fails, the service goes on with the other exclusions and logs a warning.

### 7.2 Filling

The places are visited in the order of section 6.2. For a place of topic `t`, take the best-ranked candidate of `t` that respects:

1. **not the same story** as an article already placed in the batch. Two articles cover the same story if they have the same non-null `cluster_id` or if `sim ≥ DUP_SIM`. The backend has no `cluster_id` column: the service always leaves it null, and similarity alone counts. The code keeps the `cluster_id` comparison, used by the tests and the simulation, for the day the ingestion produces some;
2. **varied sources**: among the 9 articles placed just before, at most `SOURCE_MAX − 1` come from the same source (that is at most `SOURCE_MAX` per window of 10).

Two additions to these rules:

- **liked or rejected articles**: a candidate that covers the same story as a liked or rejected article of the profile (4.3) is never placed. Only `sim ≥ DUP_SIM` is compared here, the `cluster_id` of these articles not being loaded. Without it, right after a like, closeness would put first the same story told by another outlet;
- **kept stock**: the cards of `exclude`, in the request's order, count as placed before position 0. They mark their stories before the filling (rule 1), and the window of the 9 previous articles starts filled with the sources of the stock's last 9 cards (rule 2). The reader sees them just before the batch: without this, both rules would only hold inside the batch, not in the feed actually served.

If no candidate of `t` respects both rules, take the best-ranked one that respects rule 1 alone (and mark it `relaxedSource`). If there is none either, the topic is exhausted: this place and the topic's next ones are skipped, which spreads its places over the others in proportion.

Stop when the batch holds `count` articles or all topics are exhausted. The batch can therefore be shorter than asked for, which the contract allows.

### 7.3 The `reason` field

| Value | Condition |
| --- | --- |
| `similar` | closeness ≥ `AFF_REASON_MIN` |
| `onboarding` | otherwise, and the main topic is a chosen topic |
| `explore` | otherwise |

`collaborative` and `trending` are never sent in v1.

## 8. Service

The rules the service must hold; how it does so is in [Architecture](architecture.md) and [Flows](flows.md).

### 8.1 General shape

Two processes from one package: **api** (`POST /v1/recommendations`, explanation routes, in-memory catalogue) and **worker** (encoding, fallback lists, purge, `events` stream). The computation of sections 4 to 7 is written as pure functions, without access to the database, Redis or the clock: these dependencies are passed as arguments, which makes the tests of section 11 possible without infrastructure.

### 8.2 Catalogue

The recommendable articles are those with an embedding of the current model in `reco.article_embeddings`, `published_at` within the last `MAX_AGE_DAYS` days, an active source and a main topic (`cluster_id` is null, 7.2). The catalogue is reloaded in full every `CATALOG_REFRESH_S` seconds and replaced atomically. Similarities are computed exactly, without an approximate index. An empty catalogue gives `items: []`, and the backend serves its fallback feed.

### 8.3 `POST /v1/recommendations`

Request and response formats: those of the contract, unchanged.

- `requestId`: `r_` followed by a UUID v4 in hexadecimal; `modelVersion`: `topic-weights-v1`.
- `items`: in display order (the assembly's), not by score.
- 400 if the request is invalid; 404 if `userId` does not exist in `app.users`; 503 if the database is unreachable. A new reader who has topics gets a real batch from the first call, built on their starting weights.
- Latency: 95 % of complete responses (reading, computing, saving the batch) under 150 ms with 5,000 articles and 300 events for the reader; the backend gives up at 300 ms. The computation alone must take under 80 ms (test A13).

### 8.4 Encoding

Every `ENCODE_INTERVAL_S` seconds, up to 64 articles without an embedding of the current model, the most recently ingested first. Model `intfloat/multilingual-e5-small` (384 dimensions) on CPU; text `query: {title}. {summary}`, HTML removed from the summary, truncated to 512 tokens; vector normalised to 1. Nothing is written to `app`: the embeddings go to `reco.article_embeddings` ([Database](database.md)).

### 8.5 Fallback lists

Every 10 minutes, for each topic, the 50 most recent articles of active sources whose main topic it is, published within the last `MAX_AGE_DAYS` days, in the Redis list `fallback:{slug}` with a TTL of 1 hour, replaced atomically.

### 8.6 Stored profiles and the `events` stream

Contract v1.5. The worker reads the `events` stream in the consumer group `reco` and, for each reader with a new event, recomputes the profile from `app.events` (never from the message) and stores the result of the replay (4.2, 4.3) in `reco.user_profiles` with its mark; on `user_deleted` it deletes the reader's profile and batches at once. A request uses the stored profile only if its mark (number of profile events, last `occurred_at` and `event_id`) and its basis (model version, parameters, topics, chosen topics) match the moment; otherwise it replays `app.events` and stores the result. The age factor (4.3) and the echo topic (4.4) are always computed at the request. Details: [Flows](flows.md#a-strong-signal).

### 8.7 Purge

Every hour: batches older than 30 days, batches and profiles of readers gone from `app.users`, embeddings of articles gone from `app.articles`. The recommender keeps no other personal data.

## 9. Explanation and traceability

### 9.1 Saved batches

Every batch is saved (`reco.feed_requests`, `reco.feed_items`, [Database](database.md)) with:

- `profile`: `{ "weights": {slug: weight}, "shares": {slug: share after the cap}, "likedCount": n, "rejectedCount": n, "echoTopic": slug or null, "mu": value }`;
- `params`: the parameters of section 10 as used for this batch;
- per card, `explanation`: `{ "affinity": x, "freshness": x, "penalty": x, "topicWeight": x, "nearestLiked": [{ "articleId": …, "sim": x }] (3 at most), "nearestRejected": { "articleId": …, "sim": x } or null, "relaxedSource": bool }`.

### 9.2 Read routes

Unauthenticated, reachable from the internal network only.

| Route | Content |
| --- | --- |
| `GET /v1/explain/users/{userId}` | Topics (id, slug, label, chosen or not, weight, share), liked and rejected articles with their strength, the last 50 events that changed a weight: type, article, topic, δ, weight of the topic after |
| `GET /v1/explain/requests/{requestId}` | The saved batch and its cards, with the title of each article |
| `GET /healthz` | Status, size and age of the catalogue |

## 10. Parameters

All in a single configuration module (`params.py`), overridable by environment variables (`RECO_<NAME>`).

| Name | Value | Role |
| --- | --- | --- |
| `EPSILON` | 0.10 | Total share of the unchosen topics at start |
| `W_FLOOR` | 0.01 | Floor of a weight, except a hide on an unchosen topic |
| `P_MAX` | 0.60 | Largest share of a topic in a batch |
| `T_SKIP` | 1,500 ms | Below it, the card is skipped |
| `T_LONG` | 6,000 ms | Above it, a long view |
| `V_SKIP` | −0.02 | Value of a skipped article |
| `V_LONG_VIEW` | 0.02 | Long view |
| `V_OPEN` | 0.03 | Open |
| `V_READ_BONUS` | 0.04 | Added to `V_OPEN`, in proportion to the read |
| `V_LIKE` | 0.08 | Like, save or share |
| `V_HIDE` | −0.15 | Hide |
| `S_SKIP` | 0.15 | Strength of a skipped article in the rejected list |
| `HALF_LIFE_PROFILE_DAYS` | 30 | Half-life of the liked and rejected articles |
| `PROFILE_MAX_ITEMS` | 100 | Largest size of each list |
| `EVENTS_MAX` | 5,000 | Most events replayed |
| `W_AFF` | 0.6 | Weight of closeness in the score |
| `W_FRESH` | 0.4 | Weight of freshness |
| `W_NEG` | 0.5 | Weight of rejection |
| `HALF_LIFE_FRESH_HOURS` | 48 | Half-life of freshness |
| `MAX_AGE_DAYS` | 14 | Largest age of a recommendable article |
| `DUP_SIM` | 0.80 | "Same story" threshold |
| `SOURCE_MAX` | 2 | Articles of one source per window of 10 |
| `AFF_REASON_MIN` | 0.20 | Smallest closeness for `reason = similar` |
| `CATALOG_REFRESH_S` | 60 | Reload period of the catalogue |
| `ENCODE_INTERVAL_S` | 15 | Encoding period |

`DUP_SIM` and `AFF_REASON_MIN` depend on the embedding model: they are to be checked on real articles now that the ingestion is in place (about twenty pairs judged by hand are enough).

## 11. Evaluation with simulated readers

### 11.1 Synthetic catalogue

To test without a model or a database: 10 topics, 3 subtopics per topic, 8 sources. Each topic has a centre drawn at random on the unit sphere in dimension 384; each subtopic is `normalise(topic centre + 0.6 × noise)`; each article is `normalise(subtopic centre + 0.7 × noise)`, the noise being a Gaussian vector of norm 1. With these values, two articles of the same subtopic have a similarity of about 0.65: enough to be brought together, not enough to pass for the same story. 60 articles per subtopic, sources given in turn, fixed seed.

### 11.2 Acceptance tests

All deterministic (seed and clock imposed). The bounds of A1, A2 and A4 hold for the seed the tests impose: from one seed to another, the random offsets of 6.2 move the number of cards of a topic by about 2 cards either way around its share. Unless stated otherwise, the articles all have the same age (24 h), and the reader chose 3 topics out of 10.

| # | Scenario | Expected |
| --- | --- | --- |
| A1 | New reader, batch of 50 | Between 40 and 48 cards of the chosen topics; each chosen topic has between 13 and 17 cards |
| A2 | Two likes on two articles of an unchosen topic X, then a hide on an article of an unchosen topic Y; batch of 50 | Weights equal to those of the example in 4.2 within 0.001; position 0 of the batch in topic X; no card of Y; at least 6 of the first 10 in the chosen topics |
| A3 | Five likes on five articles of an unchosen topic | Weight of the topic ≥ 0.30 |
| A4 | Twenty likes on one topic, batch of 50 | Computed share of the topic equal to `P_MAX`; at most 31 cards of the topic in the batch with the imposed seed (30 expected); at least 3 topics present |
| A5 | Three likes on subtopic S of a chosen topic T | Among the cards of T in the next batch, at least 70 % are of S |
| A6 | Hide on an article of subtopic S of a chosen topic T | The weight of T goes down and stays ≥ `W_FLOOR`; among the cards of T in the next batch, at most 15 % are of S |
| A7 | Long view, open, complete read then like on one article | Total gain of the topic before renormalisation equal to `V_LIKE`, not the sum |
| A8 | Like then `unlike` on an article without any other signal | Value of the article back to 0 |
| A9 | Twenty successive batches, each added to `seen` | No article twice; no article of `exclude` returned |
| A10 | Batch of 50 | No "same story" pair; at most 2 articles per source in any window of 10 |
| A11 | Same state, same seed, same clock, two calls | Identical batches |
| A12 | Articles of various ages, reader without interaction | Within each topic, output from the most recent to the oldest |
| A13 | 5,000 articles, 300 events, 200 calls of the pure computation (no database nor Redis) | 95 % under 80 ms (performance test, run apart) |

### 11.3 Report for the jury

A script plays 4 typical readers over 100 cards each, imitating the backend (pages of 10, the n+10 stock rule decided with the backend; the current backend differs), and produces a table per reader and per page of 10:

- share of relevant cards (within the simulated reader's stated tastes);
- share of the favourite topic;
- share of exploration (`reason = explore`);
- number of distinct topics and sources.

Typical readers: faithful to their chosen topics; passionate about an unchosen topic; allergic to a chosen topic; a reader who skips everything. Each reader follows a fixed rule (likes such subtopic, hides such topic, skips the rest). Output in Markdown and CSV.

### 11.4 Success of each reason on real readers

`python -m swipread_reco reasons [--days N]` (30 days by default, the retention of the batches) prints, for each `reason`, what real readers did with the cards they were shown. A card is an `impression` event whose `requestId` and `articleId` are an item of a stored batch (`reco.feed_items` gives its reason), or whose `requestId` is `fallback` (the backend's fallback feed, row `fallback`, for comparison). Each card counts once per batch, article and reader.

| Column | Meaning |
| --- | --- |
| Cartes | Cards shown |
| Lecteurs | Distinct readers |
| Ouvertes | Share of the cards followed by an `open` or a `read` |
| Aimées, sauvegardées ou partagées | Share followed by a `like`, `save` or `share` |
| Masquées | Share followed by a `hide` |

Exploration succeeds when its cards are opened and kept about as often as the others, and hidden not much more. The table reads only `app.events` of the period and `reco.feed_items`; it writes nothing.
