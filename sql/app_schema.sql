-- Schema `app`, copied from docs/database.md (the authoritative DDL), for local dev and tests.
-- The `reco` tables are created by the service itself (PgStore.ensure_schema).

CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE app.users (
  id            text PRIMARY KEY,              -- u_...
  email         text NOT NULL UNIQUE,
  password_hash text NOT NULL,
  display_name  text NOT NULL,
  locale        text NOT NULL DEFAULT 'fr-FR',
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app.topics (
  id    serial PRIMARY KEY,
  slug  text NOT NULL UNIQUE,
  label text NOT NULL
);

CREATE TABLE app.user_topics (
  user_id    text REFERENCES app.users(id) ON DELETE CASCADE,
  topic_id   int  REFERENCES app.topics(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, topic_id)
);

CREATE TABLE app.sources (
  id       serial PRIMARY KEY,
  name     text NOT NULL,
  site_url text NOT NULL,
  feed_url text NOT NULL UNIQUE,
  language text NOT NULL,
  license  text,
  active   boolean NOT NULL DEFAULT true
);

CREATE TABLE app.articles (
  id              text PRIMARY KEY,            -- a_...
  source_id       int  NOT NULL REFERENCES app.sources(id),
  url             text NOT NULL UNIQUE,
  title           text NOT NULL,
  summary         text NOT NULL,
  image_url       text,
  author          text,
  language        text NOT NULL,               -- code à deux lettres, ex. fr
  word_count      int  NOT NULL,
  status          text NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'expired', 'removed')),
  content_hash    text,
  cluster_id      text,
  published_at    timestamptz NOT NULL,
  ingested_at     timestamptz NOT NULL DEFAULT now(),
  embedding       vector(384),
  embedding_model text,
  embedded_at     timestamptz
);
CREATE INDEX articles_published_idx ON app.articles (published_at DESC);
CREATE INDEX articles_to_embed_idx ON app.articles (ingested_at DESC)
  WHERE embedding IS NULL;

CREATE TABLE app.article_topics (
  article_id text REFERENCES app.articles(id) ON DELETE CASCADE,
  topic_id   int  REFERENCES app.topics(id),
  confidence real NOT NULL,
  PRIMARY KEY (article_id, topic_id)
);

CREATE TABLE app.events (
  event_id    text NOT NULL,                   -- e_..., généré par le front
  user_id     text NOT NULL,
  session_id  text NOT NULL,
  type        text NOT NULL
              CHECK (type IN ('impression', 'view', 'open', 'read', 'like',
                              'unlike', 'save', 'share', 'hide')),
  article_id  text NOT NULL,
  request_id  text,
  position    int,
  payload     jsonb NOT NULL DEFAULT '{}',
  occurred_at timestamptz NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (event_id, occurred_at)
) PARTITION BY RANGE (occurred_at);
CREATE INDEX events_user_idx ON app.events (user_id, occurred_at DESC);
CREATE INDEX events_request_idx ON app.events (request_id);
-- Catch-all partition; production creates one partition per month.
CREATE TABLE app.events_default PARTITION OF app.events DEFAULT;

CREATE TABLE app.user_articles (
  user_id        text REFERENCES app.users(id) ON DELETE CASCADE,
  article_id     text REFERENCES app.articles(id) ON DELETE CASCADE,
  first_seen_at  timestamptz NOT NULL,
  total_dwell_ms int NOT NULL DEFAULT 0,
  max_scroll     real,
  liked_at       timestamptz,
  saved_at       timestamptz,
  hidden_at      timestamptz,
  PRIMARY KEY (user_id, article_id)
);
CREATE INDEX user_articles_saved_idx ON app.user_articles (user_id, saved_at DESC)
  WHERE saved_at IS NOT NULL;
