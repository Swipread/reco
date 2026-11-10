"""PostgreSQL access: the only module of the service that holds SQL (docs/database.md)."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

import numpy as np
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from swipread_reco.models import CatalogRow, Event, Lot, Topic
from swipread_reco.params import MODEL_VERSION, Params

# Longest wait for a pooled connection (infrastructure setting, not a spec 10 parameter).
DEFAULT_POOL_TIMEOUT_S = 2.0

# Every event type the profile reads; `impression` is left out (spec 4.1).
PROFILE_EVENT_TYPES = ["view", "open", "read", "like", "unlike", "save", "share", "hide"]

# Main topic of each article: highest confidence, then lowest topic_id.
PRIMARY_TOPIC = """
    SELECT DISTINCT ON (article_id) article_id, topic_id
    FROM app.article_topics
    ORDER BY article_id, confidence DESC, topic_id
"""

# Recommendable articles (spec 8.2), except the embedding condition.
RECOMMENDABLE = f"""
    FROM app.articles a
    JOIN app.sources s ON s.id = a.source_id AND s.active
    JOIN ({PRIMARY_TOPIC}) p ON p.article_id = a.id
    WHERE a.status = 'active'
      AND a.published_at >= %(now)s - make_interval(days => %(max_age_days)s)
"""

RECO_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS reco;

CREATE TABLE IF NOT EXISTS reco.feed_requests (
  request_id      text PRIMARY KEY,
  user_id         text NOT NULL,
  session_id      text,
  model_version   text NOT NULL,
  requested_count int  NOT NULL,
  seed            bigint NOT NULL,
  profile         jsonb NOT NULL,
  params          jsonb NOT NULL,
  generated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS feed_requests_user_idx
  ON reco.feed_requests (user_id, generated_at DESC);

CREATE TABLE IF NOT EXISTS reco.feed_items (
  request_id  text REFERENCES reco.feed_requests(request_id) ON DELETE CASCADE,
  position    smallint,
  article_id  text NOT NULL,
  topic_id    int  NOT NULL,
  reason      text NOT NULL,
  score       real NOT NULL,
  explanation jsonb NOT NULL,
  PRIMARY KEY (request_id, position)
);
"""


class Store(Protocol):
    def ensure_schema(self) -> None: ...
    def topics(self) -> list[Topic]: ...
    def user_locale(self, user_id: str) -> str | None: ...
    def chosen_topics(self, user_id: str) -> frozenset[int]: ...
    def events(self, user_id: str, limit: int) -> list[Event]: ...
    def user_article_ids(self, user_id: str) -> frozenset[str]: ...
    def embeddings(self, ids: Sequence[str]) -> dict[str, np.ndarray]: ...
    def last_request_at(self, user_id: str) -> datetime | None: ...
    def catalog_rows(self, now: datetime, max_age_days: int) -> list[CatalogRow]: ...
    def save_lot(
        self, lot: Lot, *, user_id: str, session_id: str | None,
        requested_count: int, generated_at: datetime, params: Params,
    ) -> None: ...
    def request(self, request_id: str) -> dict[str, Any] | None: ...
    def article_titles(self, ids: Sequence[str]) -> dict[str, str]: ...
    # worker
    def articles_to_embed(self, model: str, limit: int) -> list[tuple[str, str, str]]: ...
    def write_embeddings(
        self, rows: Sequence[tuple[str, np.ndarray]], model: str, now: datetime
    ) -> None: ...
    def fallback_ids(
        self, topic_id: int, now: datetime, max_age_days: int, limit: int
    ) -> list[str]: ...
    def purge(self, now: datetime, retention_days: int) -> int: ...


def _configure(conn: Connection) -> None:
    register_vector(conn)
    conn.commit()  # register_vector reads the catalog; leave the connection idle


def _array(vector: Vector) -> np.ndarray:
    return vector.to_numpy().astype(np.float32, copy=False)


class PgStore:
    """Store backed by a psycopg connection pool, sessions in UTC, pgvector registered.

    The `vector` extension must exist before the pool connects (sql/app_schema.sql).
    A method that gets no connection within `timeout_s` raises psycopg_pool.PoolTimeout,
    so an unreachable database fails a request quickly (the backend gives up at 300 ms).
    """

    def __init__(self, dsn: str, timeout_s: float = DEFAULT_POOL_TIMEOUT_S) -> None:
        self._pool = ConnectionPool(
            dsn, kwargs={"options": "-c timezone=UTC"}, configure=_configure,
            timeout=timeout_s, open=True,
        )

    def close(self) -> None:
        self._pool.close()

    def ensure_schema(self) -> None:
        with self._pool.connection() as conn:
            conn.execute(RECO_SCHEMA)

    def topics(self) -> list[Topic]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT id, slug, label FROM app.topics ORDER BY id").fetchall()
        return [Topic(id=i, slug=slug, label=label) for i, slug, label in rows]

    def user_locale(self, user_id: str) -> str | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT locale FROM app.users WHERE id = %s", (user_id,)).fetchone()
        return row[0] if row else None

    def chosen_topics(self, user_id: str) -> frozenset[int]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT topic_id FROM app.user_topics WHERE user_id = %s", (user_id,)
            ).fetchall()
        return frozenset(r[0] for r in rows)

    def events(self, user_id: str, limit: int) -> list[Event]:
        """The reader's `limit` most recent profile events, oldest first."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                WITH recent AS (
                  SELECT event_id, type, article_id, occurred_at, received_at, payload
                  FROM app.events
                  WHERE user_id = %s AND type = ANY(%s)
                  ORDER BY occurred_at DESC, event_id DESC
                  LIMIT %s
                )
                SELECT r.event_id, r.type, r.article_id, r.occurred_at, r.received_at,
                       r.payload, p.topic_id, a.word_count
                FROM recent r
                LEFT JOIN app.articles a ON a.id = r.article_id
                LEFT JOIN LATERAL (
                  SELECT topic_id FROM app.article_topics t
                  WHERE t.article_id = r.article_id
                  ORDER BY confidence DESC, topic_id
                  LIMIT 1
                ) p ON true
                ORDER BY r.occurred_at, r.event_id
                """,
                (user_id, PROFILE_EVENT_TYPES, limit),
            ).fetchall()
        return [
            Event(
                event_id=event_id, type=type_, article_id=article_id,
                occurred_at=occurred_at, received_at=received_at, payload=payload,
                topic_id=topic_id, word_count=word_count,
            )
            for event_id, type_, article_id, occurred_at, received_at, payload, topic_id,
            word_count in rows
        ]

    def user_article_ids(self, user_id: str) -> frozenset[str]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT article_id FROM app.user_articles WHERE user_id = %s", (user_id,)
            ).fetchall()
        return frozenset(r[0] for r in rows)

    def embeddings(self, ids: Sequence[str]) -> dict[str, np.ndarray]:
        """Embeddings of the given articles; unknown or not yet encoded ones are left out."""
        if not ids:
            return {}
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, embedding FROM app.articles"
                " WHERE id = ANY(%s) AND embedding IS NOT NULL",
                (list(ids),),
            ).fetchall()
        return {i: _array(emb) for i, emb in rows}

    def last_request_at(self, user_id: str) -> datetime | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT max(generated_at) FROM reco.feed_requests WHERE user_id = %s", (user_id,)
            ).fetchone()
        return row[0]

    def catalog_rows(self, now: datetime, max_age_days: int) -> list[CatalogRow]:
        """Recommendable articles with an embedding (spec 8.2), by id."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT a.id, p.topic_id, a.source_id, lower(left(a.language, 2)),
                       a.cluster_id, a.published_at, a.embedding
                {RECOMMENDABLE}
                  AND a.embedding IS NOT NULL
                ORDER BY a.id
                """,
                {"now": now, "max_age_days": max_age_days},
            ).fetchall()
        return [
            CatalogRow(
                id=i, topic_id=topic_id, source_id=source_id, language=language,
                cluster_id=cluster_id, published_at=published_at,
                embedding=_array(emb),
            )
            for i, topic_id, source_id, language, cluster_id, published_at, emb in rows
        ]

    def save_lot(
        self, lot: Lot, *, user_id: str, session_id: str | None,
        requested_count: int, generated_at: datetime, params: Params,
    ) -> None:
        """Write the lot's feed_requests row and its feed_items in one transaction (spec 9.1)."""
        with self._pool.connection() as conn, conn.transaction():
            slug = dict(conn.execute("SELECT id, slug FROM app.topics").fetchall())
            profile = lot.profile
            profile_json = {
                "weights": {slug[t]: w for t, w in profile.weights.items()},
                "shares": {slug[t]: s for t, s in lot.shares.items()},
                "likedCount": len(profile.liked),
                "rejectedCount": len(profile.rejected),
                "echoTopic": slug[profile.echo_topic] if profile.echo_topic is not None else None,
                "mu": lot.mu,
            }
            conn.execute(
                "INSERT INTO reco.feed_requests (request_id, user_id, session_id, model_version,"
                " requested_count, seed, profile, params, generated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    lot.request_id, user_id, session_id, MODEL_VERSION, requested_count,
                    lot.seed, Jsonb(profile_json), Jsonb(params.as_dict()), generated_at,
                ),
            )
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO reco.feed_items (request_id, position, article_id, topic_id,"
                    " reason, score, explanation) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            lot.request_id, position, item.article_id, item.topic_id,
                            item.reason, item.score, Jsonb(item.explanation),
                        )
                        for position, item in enumerate(lot.items)
                    ],
                )

    def request(self, request_id: str) -> dict[str, Any] | None:
        """The feed_requests row with its `items` (by position, each with the article `title`,
        None if the article is gone); None if the request is unknown."""
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "SELECT request_id, user_id, session_id, model_version, requested_count, seed,"
                " profile, params, generated_at FROM reco.feed_requests WHERE request_id = %s",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            row["items"] = cur.execute(
                "SELECT i.position, i.article_id, i.topic_id, i.reason, i.score, i.explanation,"
                " a.title FROM reco.feed_items i LEFT JOIN app.articles a ON a.id = i.article_id"
                " WHERE i.request_id = %s ORDER BY i.position",
                (request_id,),
            ).fetchall()
        return row

    def article_titles(self, ids: Sequence[str]) -> dict[str, str]:
        if not ids:
            return {}
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, title FROM app.articles WHERE id = ANY(%s)", (list(ids),)
            ).fetchall()
        return dict(rows)

    def articles_to_embed(self, model: str, limit: int) -> list[tuple[str, str, str]]:
        """(id, title, summary) of articles not encoded by `model`, newest ingested first."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, title, summary FROM app.articles"
                " WHERE embedding IS NULL OR embedding_model IS DISTINCT FROM %s"
                " ORDER BY ingested_at DESC, id LIMIT %s",
                (model, limit),
            ).fetchall()
        return [tuple(r) for r in rows]

    def write_embeddings(
        self, rows: Sequence[tuple[str, np.ndarray]], model: str, now: datetime
    ) -> None:
        if not rows:
            return
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "UPDATE app.articles SET embedding = %s, embedding_model = %s, embedded_at = %s"
                " WHERE id = %s",
                [(np.asarray(emb, dtype=np.float32), model, now, i) for i, emb in rows],
            )

    def fallback_ids(
        self, topic_id: int, now: datetime, max_age_days: int, limit: int
    ) -> list[str]:
        """Recommendable articles whose main topic is `topic_id`, newest first (spec 8.5)."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT a.id
                {RECOMMENDABLE}
                  AND p.topic_id = %(topic_id)s
                ORDER BY a.published_at DESC, a.id
                LIMIT %(limit)s
                """,
                {"now": now, "max_age_days": max_age_days, "topic_id": topic_id, "limit": limit},
            ).fetchall()
        return [r[0] for r in rows]

    def purge(self, now: datetime, retention_days: int) -> int:
        """Delete requests older than `retention_days` or of users gone from app.users (8.7)."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "DELETE FROM reco.feed_requests r"
                " WHERE r.generated_at < %s - make_interval(days => %s)"
                " OR NOT EXISTS (SELECT 1 FROM app.users u WHERE u.id = r.user_id)",
                (now, retention_days),
            )
            return cur.rowcount
