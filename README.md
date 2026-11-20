# Swipread · Recommender

**The recommendation service of Swipread, a feed of recommended articles you scroll one card at a time.** For each reader it composes a batch of articles from the topics they chose at sign-up and from everything they did since, and it can explain every card it served, figures included.

Python 3.12 · FastAPI · PostgreSQL 17 + pgvector · Redis 7.4 · numpy · sentence-transformers (multilingual-e5-small) · uv.

[![CI](https://github.com/Swipread/reco/actions/workflows/ci.yml/badge.svg)](https://github.com/Swipread/reco/actions/workflows/ci.yml)
![Python 3.12](https://img.shields.io/badge/Python-3.12-3776ab)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688)
![PostgreSQL 17](https://img.shields.io/badge/PostgreSQL-17%20%2B%20pgvector-336791)
![Redis 7.4](https://img.shields.io/badge/Redis-7.4-dc382d)
![License](https://img.shields.io/badge/license-all%20rights%20reserved-lightgrey)

## Contents

- [How Swipread fits together](#how-swipread-fits-together)
- [Features](#features)
- [How it works](#how-it-works)
- [Getting started](#getting-started)
- [Routes](#routes)
- [Project structure](#project-structure)
- [Testing](#testing)
- [Configuration](#configuration)
- [Project status](#project-status)
- [Documentation](#documentation)
- [License](#license)

## How Swipread fits together

```mermaid
flowchart LR
    Web["<b>Web app</b><br/>Swipread/web"] -- "REST + JWT" --> API["<b>Swipread API</b><br/>Swipread/api"]
    API -- "POST /v1/recommendations<br/>300 ms timeout" --> RecoAPI["<b>Recommender api</b><br/>this repository"]
    API -- "events stream" --> Redis[(Redis<br/>events, seen, fallback lists)]
    Redis -- "group reco" --> Worker["<b>Recommender worker</b><br/>this repository"]
    RecoAPI --> PG[(PostgreSQL + pgvector<br/>app: read only · reco: own tables)]
    Worker --> PG
    Worker -- "fallback:{slug}" --> Redis
```

The [API](https://github.com/Swipread/api) owns accounts, articles and events; it serves each reader's feed from a stock in Redis and asks this service to refill it. The recommender answers with article ids only, never content, and it never writes to the API's tables: it reads them through a dedicated read-only role and keeps its own data in the `reco` schema. The [web app](https://github.com/Swipread/web) never talks to it. The interface between the API and the recommender is fixed by a contract agreed between the two teams.

## Features

| Recommendations | Explanation | Platform |
| --- | --- | --- |
| A batch of up to 100 article ids per call, in display order, each with a score and a reason (`similar`, `onboarding`, `explore`) | Every batch saved with the score breakdown of each card | Two processes from one package: `api` and `worker` |
| The reader's chosen topics as the base of the feed, their interactions reshaping it | Per-reader route: topic weights and shares, liked and rejected articles, last weight changes | The algorithm in pure functions: no database, Redis or clock inside, fully deterministic |
| A strong signal (like, save, share, complete read) puts its topic first in the next batch | Per-batch route: the saved batch with each card's title and score terms | Catalogue kept in memory, reloaded every minute |
| A hidden topic the reader never liked leaves the feed | Parameters and model version saved with every batch | Articles encoded with a multilingual embedding model on CPU |
| No repeat, no two articles on the same story, varied sources | | Reader profiles kept up to date from the API's events stream, checked against the event log at each request |
| | | Fallback lists per topic in Redis for when the recommender is down |

## How it works

**A model you can explain.** Each reader has one weight per topic, starting from the topics they chose. Every interaction gives the article a value (a quick skip is slightly negative, a long view, an open or a read positive, a like or save the most, a hide strongly negative) and moves the weight of its topic by the change. The weights decide how many cards each topic gets in a batch, capped at 60 %, interleaved so that every page of 10 keeps the proportions. → [Algorithm](docs/algorithm.md)

**A score per article.** Within a topic, articles are ranked by closeness to what the reader liked (cosine similarity of embeddings, recentred on the catalogue), freshness (48-hour half-life) and a penalty for resemblance to what they rejected. Liking an article about AI also brings closer an economy article about AI. → [Algorithm](docs/algorithm.md)

**The echo of a strong signal.** After a like, save, share or complete read, the next batch starts with that topic, unless the stock the API kept already holds a card of it. Without that exception, a series of likes would reduce the feed to one topic.

**Profiles kept fresh, never trusted blindly.** The worker reads the API's `events` stream in its own consumer group and recomputes the profile of each reader who did something, from the event log, which the API writes before publishing. A request uses the stored profile only if it was computed from the same events, topics and parameters; otherwise it replays the log. A batch asked for right after a like therefore always includes it, even if its message has not been read yet. → [Flows](docs/flows.md#a-strong-signal)

**Nothing kept on a deleted account.** On the API's `user_deleted` message, the worker deletes the reader's profile and saved batches at once; an hourly purge does the same for any account gone from the database, and drops batches older than 30 days.

## Getting started

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/) and Docker.

```bash
git clone https://github.com/Swipread/reco.git
cd reco
uv sync
docker compose up -d          # PostgreSQL 17 + pgvector on 5433, Redis 7.4 on 6380
uv run pytest -m "not perf"   # unit, API and acceptance tests
```

The service needs the API's schema (its Flyway migrations create the `app` and `reco` schemas and the recommender's role): to see it serve batches, run it next to the API, as described in [Getting started](docs/getting-started.md). Once both run:

```bash
uv run python -m swipread_reco worker   # encodes articles, writes fallback lists, reads the stream
uv run python -m swipread_reco api      # listens on 127.0.0.1:8081
uv run python -m swipread_reco reasons  # success of each reason on real readers, last 30 days
curl -s localhost:8081/healthz
```

## Routes

The routes are not authenticated: the api must only be reachable from the internal network.

| Route | |
| --- | --- |
| `POST /v1/recommendations` | A batch for a reader: `userId`, `count`, `exclude` (the stock the API keeps), `context` |
| `GET /healthz` | Status, size and age of the in-memory catalogue |
| `GET /v1/explain/users/{userId}` | Topic weights and shares, liked and rejected articles, last 50 weight changes |
| `GET /v1/explain/requests/{requestId}` | A saved batch, with the score breakdown of each card |

The request and response formats are the API's: [API reference](https://github.com/Swipread/api/blob/main/docs/api.md).

## Project structure

```text
src/swipread_reco/
├── __main__.py       entry point: python -m swipread_reco api | worker | reasons
├── api.py            POST /v1/recommendations, /healthz
├── explain.py        explanation routes
├── reasons.py        success of each reason on real readers (spec 11.4)
├── recommend.py      the pipeline: profile, score, allocation, assembly
├── profile.py        replay of the events into a profile (pure)
├── profiles.py       stored profile: fresh, or replayed and stored again
├── scoring.py        closeness, freshness, penalty
├── allocation.py     shares per topic and their order in the batch
├── assemble.py       filling the batch: same story, varied sources
├── catalog_cache.py  in-memory catalogue, reloaded in the background
├── params.py         every tunable parameter, overridable by environment
├── models.py         shared data types
├── store.py          all the SQL, behind the Store protocol
├── worker/           encoding, fallback lists, purge, events stream consumer
└── sim/              simulated catalogue and readers, report for the jury
sql/app_schema.sql    copy of the API's migrations, for the integration tests
scripts/e2e.sh        end-to-end run against the real API
```

## Testing

```bash
uv run pytest -m "not perf"   # 221 unit, API and acceptance tests, no infrastructure
uv run ruff check
RECO_TEST_DSN=postgresql://postgres:postgres@localhost:5433/postgres uv run pytest tests/integration -q
```

The 28 integration tests rebuild the API's schema in a disposable database and run as a member of the recommender's role, which proves its rights are enough. GitHub Actions runs the lint, the unit and the integration tests on every push and pull request. Apart: a performance test (`-m perf`, 80 ms at the 95th percentile for 5,000 articles) and 11 end-to-end scenarios against the real API (`scripts/e2e.sh`). → [Testing](docs/testing.md)

A report on four simulated readers over 100 cards, page by page, is written by `uv run python -m swipread_reco.sim.report --out report/`.

## Configuration

Both processes read their settings from environment variables: `RECO_DATABASE_URL`, `RECO_REDIS_URL`, `RECO_DB_TIMEOUT_S`; the api also `RECO_HOST` and `RECO_PORT`, the worker `RECO_STREAM_CONSUMER`. Every parameter of the algorithm can be overridden with `RECO_<NAME>`, for instance `RECO_P_MAX=0.5`. → [Configuration](docs/configuration.md)

## Project status

The algorithm of the specification is implemented with its acceptance tests and explanation routes, and the interface with the API follows contract v1.7: the recommender consumes the events stream and keeps reader profiles. It runs next to the real API in the end-to-end tests. Version 1.1.0 is in production: a version tag on `main` runs the checks, builds the container image (CPU-only torch, embedding model included), deploys it next to the API and creates the GitHub release. → [Deployment](docs/deployment.md)

## Documentation

| | |
| --- | --- |
| **Guides** | [Getting started](docs/getting-started.md) · [Architecture](docs/architecture.md) · [Flows](docs/flows.md) · [Database](docs/database.md) · [Configuration](docs/configuration.md) · [Deployment](docs/deployment.md) · [Testing](docs/testing.md) · [FAQ](docs/faq.md) · [Glossary](docs/glossary.md) |
| **Reference** | [Algorithm](docs/algorithm.md) |

Start from the [documentation index](docs/README.md).

## License

© 2026 Swipread. All rights reserved.

The source is published for reference only; no license is granted to use, copy, modify or distribute it without written permission from Swipread.
