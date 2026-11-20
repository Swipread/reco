# Glossary

| Term | Meaning |
| --- | --- |
| **Acceptance tests** (A1–A13) | The scenarios of spec section 11.2, run on a synthetic catalogue with a fixed seed and clock |
| **Age factor** | `0.5 ^ (age_days / 30)` applied to the strength of a liked or rejected article, from its last event |
| **Basis** | What a stored profile was computed on besides the events: model version, parameters, topics, chosen topics |
| **Batch** ("lot") | The list of article ids returned by one `POST /v1/recommendations` call, saved in `reco.feed_requests` |
| **Catalogue** | The recommendable articles (embedded, recent, of an active source, with a main topic), kept in memory by the api |
| **Closeness** (`affinity`) | Mean of the 3 best strength × similarity values between a candidate and the liked articles |
| **Complete read** | A `read` the API judges strong: `scrollDepth > 0.8`, or `readMs` at least 60 % of the expected reading time |
| **Contract** | The interface agreement between the API and the recommender, versioned (v1.6), internal to the teams |
| **Echo topic** | Topic of the latest strong positive signal since the previous batch, placed first in the next one |
| **Embedding** | 384-number unit vector of an article (`multilingual-e5-small` on `query: title. summary`), in `reco.article_embeddings` |
| **Exclude** | The cards still in the API's stock, sent with each request: never recommended again, counted as placed before the batch |
| **Fallback list** | Redis list `fallback:{slug}` of the 50 newest articles of a topic, written by the worker, served by the API when the recommender fails |
| **Freshness** | `0.5 ^ (age_hours / 48)` from `published_at` |
| **Main topic** | The topic of highest `confidence` of an article in `app.article_topics`, lowest id on a tie |
| **Mark** | Where a stored profile stands in `app.events`: number of profile events, last `occurred_at` and `event_id` |
| **`mu`** | Mean cosine between two random articles of the catalogue, used to recentre similarities |
| **Penalty** (rejection) | Highest strength × similarity between a candidate and the rejected articles |
| **Profile** | Weights per topic, liked and rejected articles and the echo topic of a reader, replayed from their events |
| **Reason** | Why a card was recommended: `similar`, `onboarding` or `explore` |
| **Replay** | Folding the reader's events, oldest first, into the profile |
| **Request id** (`requestId`) | Id of a batch (`r_…`); its SHA-256 seeds the random offsets |
| **Same story** | Two articles with a similarity of at least `DUP_SIM` (0.80): never both in a batch |
| **Spec** | What the code's comments call the [algorithm](algorithm.md) page, whose section numbers ("spec 4.2") are stable |
| **Share** | Part of the batch given to a topic: its weight, capped at 60 % |
| **Stored profile** | The row of `reco.user_profiles`: replay, mark and basis |
| **Strong event** | `like`, `save`, `share`, `hide` or complete `read`: the API trims and refills the stock |
| **Value** (`v`) | What an article is worth to the reader, from its strongest signal (−0.15 for a hide to 0.08 for a like) |
| **Weight** | A topic's part of the reader's interest; weights sum to 1 |
