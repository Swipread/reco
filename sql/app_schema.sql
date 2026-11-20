-- Mirror, for the reco's integration tests, of what the backend's Flyway migrations create
-- (Swipread/api at commit 3ec4afa, src/main/resources/db/migration):
--   V1__init_schema.sql                 extension, schemas app and reco, tables, indexes
--   V2__events_default_partition.sql    app.events_default
--   V3__user_role.sql                   app.users.role
--   V4__user_articles_opened_at.sql     app.user_articles.opened_at (its backfill UPDATE is left
--                                       out: it does nothing on an empty database)
--   V5__reco_role.sql                   group role swipread_reco and its privileges
--   V6__event_outbox.sql                app.event_outbox, out of the reco's reach
--   V7__refresh_tokens.sql              app.refresh_tokens, out of the reco's reach
--   V8__ingestion_and_reco_tables.sql   app.source_topics, read state of app.sources, drop of
--                                       reco.user_profiles and reco.model_runs (contract v1.5)
--   V9__drop_article_embedding_and_check_events.sql
--                                       drop of app.articles.embedding and its index, NOT NULL
--                                       app.events.article_id, CHECK on app.events.type
--   V10__article_word_count_from_summary.sql
--                                       app.articles.word_count_from_summary (its backfill UPDATE
--                                       is left out: it does nothing on an empty database)
--   V11__mail_outbox.sql                app.mail_outbox, out of the reco's reach
--   V12__email_verification.sql        app.users.email_verified_at, app.user_tokens (out of reach)
-- Statements are copied as they are, in migration order; keep in sync with new migrations.
-- The backend's tables are the reference: never add here what its migrations do not create.
-- The reco's own tables are created by the service itself (PgStore.ensure_schema).

-- V1
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA app;
CREATE SCHEMA reco;

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
  license  text,                               -- conditions de réutilisation
  active   boolean NOT NULL DEFAULT true
);

CREATE TABLE app.articles (
  id           text PRIMARY KEY,               -- a_...
  source_id    int  NOT NULL REFERENCES app.sources(id),
  url          text NOT NULL UNIQUE,
  title        text NOT NULL,
  summary      text NOT NULL,
  image_url    text,
  author       text,
  language     text NOT NULL,
  word_count   int  NOT NULL,
  published_at timestamptz NOT NULL,
  ingested_at  timestamptz NOT NULL DEFAULT now(),
  embedding    vector(384)
);
CREATE INDEX articles_published_idx ON app.articles (published_at DESC);
CREATE INDEX articles_embedding_idx ON app.articles
  USING hnsw (embedding vector_cosine_ops);

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
  type        text NOT NULL,
  article_id  text,
  request_id  text,
  position    int,
  payload     jsonb NOT NULL DEFAULT '{}',
  occurred_at timestamptz NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (event_id, occurred_at)
) PARTITION BY RANGE (occurred_at);
CREATE INDEX events_user_idx ON app.events (user_id, occurred_at DESC);
CREATE TABLE app.events_2026_10 PARTITION OF app.events
  FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');

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

CREATE TABLE reco.user_profiles (
  user_id      text PRIMARY KEY,
  embedding    vector(384) NOT NULL,
  events_count int NOT NULL DEFAULT 0,
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE reco.model_runs (
  id          serial PRIMARY KEY,
  version     text NOT NULL,                   -- ex. content-v1
  started_at  timestamptz NOT NULL,
  finished_at timestamptz,
  metrics     jsonb
);

-- V2
CREATE TABLE app.events_default PARTITION OF app.events DEFAULT;

-- V3
ALTER TABLE app.users
  ADD COLUMN role text NOT NULL DEFAULT 'READER'
  CONSTRAINT users_role_check CHECK (role IN ('READER', 'ADMIN'));

-- V4
ALTER TABLE app.user_articles ADD COLUMN opened_at timestamptz;

CREATE INDEX user_articles_opened_idx ON app.user_articles (user_id, opened_at DESC)
  WHERE opened_at IS NOT NULL;

-- V5
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'swipread_reco') THEN
    IF NOT (SELECT rolcreaterole OR rolsuper FROM pg_roles WHERE rolname = current_user) THEN
      RAISE EXCEPTION 'Role swipread_reco is missing and % cannot create it: run "CREATE ROLE swipread_reco NOLOGIN" as an administrator first', current_user;
    END IF;
    CREATE ROLE swipread_reco NOLOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA app TO swipread_reco;
GRANT SELECT ON ALL TABLES IN SCHEMA app TO swipread_reco;
ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT SELECT ON TABLES TO swipread_reco;

REVOKE SELECT ON app.users FROM swipread_reco;
GRANT SELECT (id, locale, created_at) ON app.users TO swipread_reco;

GRANT USAGE, CREATE ON SCHEMA reco TO swipread_reco;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA reco TO swipread_reco;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA reco TO swipread_reco;
ALTER DEFAULT PRIVILEGES IN SCHEMA reco GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO swipread_reco;
ALTER DEFAULT PRIVILEGES IN SCHEMA reco GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO swipread_reco;

-- V6
CREATE TABLE app.event_outbox (
  id         bigserial PRIMARY KEY,
  message    text NOT NULL,                   -- JSON du message, tel que publié dans le champ data du stream
  created_at timestamptz NOT NULL DEFAULT now()
);

REVOKE ALL ON app.event_outbox FROM swipread_reco;

-- V7
CREATE TABLE app.refresh_tokens (
  id          uuid PRIMARY KEY,
  user_id     text NOT NULL REFERENCES app.users(id) ON DELETE CASCADE,
  family_id   uuid NOT NULL,
  token_hash  text NOT NULL UNIQUE,            -- SHA-256 en hexadécimal
  created_at  timestamptz NOT NULL,
  expires_at  timestamptz NOT NULL,
  revoked_at  timestamptz,
  replaced_by uuid REFERENCES app.refresh_tokens(id) ON DELETE SET NULL
);
CREATE INDEX refresh_tokens_family_idx ON app.refresh_tokens (family_id);
CREATE INDEX refresh_tokens_expires_idx ON app.refresh_tokens (expires_at);

REVOKE ALL ON app.refresh_tokens FROM swipread_reco;

-- V8
-- Contrat v1.5 : ingestion des sources et tables de la reco.

-- Thèmes de chaque source, hérités par ses articles. position = ordre choisi par l'admin :
-- l'article reçoit confidence 1.0, 0.9, 0.8 dans cet ordre, le premier est donc son thème principal.
CREATE TABLE app.source_topics (
  source_id int      NOT NULL REFERENCES app.sources(id) ON DELETE CASCADE,
  topic_id  int      NOT NULL REFERENCES app.topics(id),
  position  smallint NOT NULL CHECK (position BETWEEN 1 AND 3),
  PRIMARY KEY (source_id, topic_id),
  UNIQUE (source_id, position)
);

-- Lecture conditionnelle du flux et état de la dernière lecture
ALTER TABLE app.sources
  ADD COLUMN etag            text,
  ADD COLUMN last_modified   text,
  ADD COLUMN last_fetched_at timestamptz,
  ADD COLUMN last_error      text;

-- Le schéma reco appartient à la reco, qui crée ses tables : celles de V1, jamais utilisées et d'un
-- format qui n'est pas celui de son profil, sont supprimées.
DROP TABLE reco.user_profiles;
DROP TABLE reco.model_runs;

-- La reco calcule et garde ses vecteurs dans reco.article_embeddings : la colonne embedding de app.articles et
-- son index, prévus au départ, n'ont jamais été remplis ni lus.
DROP INDEX app.articles_embedding_idx;
ALTER TABLE app.articles DROP COLUMN embedding;

-- L'API exige un article et un type connu pour chaque événement : la base l'impose aussi.
ALTER TABLE app.events ALTER COLUMN article_id SET NOT NULL;
ALTER TABLE app.events ADD CONSTRAINT events_type_check
  CHECK (type IN ('impression', 'view', 'open', 'read', 'like', 'unlike', 'save', 'unsave', 'share', 'hide'));

-- Vrai quand la page de l'article n'a pas pu être lue (robots.txt, défi anti-robot…) et que le flux ne donne que le
-- titre et le résumé : word_count est alors bien trop petit pour estimer un temps de lecture, et aucune lecture
-- complète ne s'en déduit.
ALTER TABLE app.articles ADD COLUMN word_count_from_summary boolean NOT NULL DEFAULT false;

-- Mails à envoyer : écrits dans la transaction qui les produit (inscription, changement de mot de passe…), puis
-- envoyés en SMTP par MailRelay, qui supprime chaque ligne envoyée. Un échec est retenté plus tard ; après la
-- dernière tentative, la ligne est marquée failed_at et effacée au bout de 7 jours.
CREATE TABLE app.mail_outbox (
  id              bigserial PRIMARY KEY,
  recipient       text NOT NULL,
  subject         text NOT NULL,
  text_body       text NOT NULL,
  html_body       text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  attempts        int NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_error      text,
  failed_at       timestamptz
);
CREATE INDEX mail_outbox_due_idx ON app.mail_outbox (next_attempt_at) WHERE failed_at IS NULL;

-- Adresses et liens personnels : la reco n'y a pas accès (droits par défaut posés par V5)
REVOKE ALL ON app.mail_outbox FROM swipread_reco;

-- Vérification de l'adresse mail : date de la confirmation, NULL tant que le lecteur n'a pas cliqué le lien.
-- Les comptes existants commencent non vérifiés. La reco n'y a pas accès (droits par colonne de V5).
ALTER TABLE app.users ADD COLUMN email_verified_at timestamptz;

-- Liens à usage unique envoyés par mail (vérification de l'adresse, mot de passe oublié), jamais stockés en clair :
-- seulement leur SHA-256.
CREATE TABLE app.user_tokens (
  id         uuid PRIMARY KEY,
  user_id    text NOT NULL REFERENCES app.users(id) ON DELETE CASCADE,
  purpose    text NOT NULL CONSTRAINT user_tokens_purpose_check CHECK (purpose IN ('verify_email', 'reset_password')),
  token_hash text NOT NULL UNIQUE,              -- SHA-256 en hexadécimal
  created_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  used_at    timestamptz
);
CREATE INDEX user_tokens_user_idx ON app.user_tokens (user_id, purpose, created_at DESC);

REVOKE ALL ON app.user_tokens FROM swipread_reco;
