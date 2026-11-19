"""PostgreSQL access: the only module of the service that holds SQL (docs/database.md)."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

import numpy as np
import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from swipread_reco.models import (
    CatalogRow,
    Event,
    EventMark,
    Lot,
    ProfileItem,
    ProfileState,
    StoredProfile,
    Topic,
    WeightChange,
)
from swipread_reco.params import EMBEDDING_MODEL, MODEL_VERSION, Params

# Longest wait for a pooled connection (infrastructure setting, not a spec 10 parameter).
DEFAULT_POOL_TIMEOUT_S = 2.0

# Every event type the profile reads; `impression` is left out (spec 4.1).
PROFILE_EVENT_TYPES = [
    "view", "open", "read", "like", "unlike", "save", "unsave", "share", "hide",
]

# Main topic of each article: highest confidence, then lowest topic_id.
PRIMARY_TOPIC = """
    SELECT DISTINCT ON (article_id) article_id, topic_id
    FROM app.article_topics
    ORDER BY article_id, confidence DESC, topic_id
"""

# Recommendable articles (spec 8.2), except the embedding condition: FROM part, WHERE part.
# The backend has no article status: every article of an active source counts as active.
RECOMMENDABLE_FROM = f"""
    FROM app.articles a
    JOIN app.sources s ON s.id = a.source_id AND s.active
    JOIN ({PRIMARY_TOPIC}) p ON p.article_id = a.id
"""
RECOMMENDABLE_WHERE = """
    WHERE a.published_at >= %(now)s - make_interval(days => %(max_age_days)s)
"""

# The service's own tables. Schema `reco` and the `vector` extension come from the backend's
# migration V1; the restricted role (V5) may create tables in `reco` but not schemas.
# Advisory lock taken by ensure_schema (any constant shared by the two processes).
SCHEMA_LOCK_KEY = 0x5245434F  # "RECO"

RECO_SCHEMA = """
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

-- The reader's replayed profile (contract v1.5), kept up to date from the `events` stream and
-- checked against app.events at each request. state and basis: see docs/database.md.
CREATE TABLE IF NOT EXISTS reco.user_profiles (
  user_id          text PRIMARY KEY,
  basis            jsonb NOT NULL,
  state            jsonb NOT NULL,
  events_count     int  NOT NULL,
  last_occurred_at timestamptz,
  last_event_id    text,
  computed_at      timestamptz NOT NULL
);

-- Embeddings live here: the role cannot write to app (no FK either, no REFERENCES right on app).
CREATE TABLE IF NOT EXISTS reco.article_embeddings (
  article_id  text PRIMARY KEY,
  embedding   vector(384) NOT NULL,
  model       text NOT NULL,
  embedded_at timestamptz NOT NULL
);
"""


class SchemaError(RuntimeError):
    """The database lacks what the backend's migrations should have created."""


class Store(Protocol):
    def ensure_schema(self) -> None: ...
    def topics(self) -> list[Topic]: ...
    def user_locale(self, user_id: str) -> str | None: ...
    def chosen_topics(self, user_id: str) -> frozenset[int]: ...
    def events(self, user_id: str, limit: int) -> list[Event]: ...
    def events_received_after(
        self, user_id: str, after: datetime | None, limit: int
    ) -> list[Event]: ...
    def event_mark(self, user_id: str) -> EventMark: ...
    def profile_inputs(self, user_id: str, limit: int) -> tuple[EventMark, list[Event]]: ...
    def stored_profile(self, user_id: str) -> StoredProfile | None: ...
    def save_profile(self, user_id: str, profile: StoredProfile, computed_at: datetime) -> None: ...
    def delete_user_data(self, user_id: str) -> None: ...
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


def lot_profile_json(lot: Lot, slug: Mapping[int, str]) -> dict[str, Any]:
    """The `profile` column of feed_requests (spec 9.1), topics named by their slug."""
    profile = lot.profile
    return {
        "weights": {slug[t]: w for t, w in profile.weights.items()},
        "shares": {slug[t]: s for t, s in lot.shares.items()},
        "likedCount": len(profile.liked),
        "rejectedCount": len(profile.rejected),
        "echoTopic": slug[profile.echo_topic] if profile.echo_topic is not None else None,
        "mu": lot.mu,
    }


def _items_json(items: Sequence[ProfileItem]) -> list[list[Any]]:
    return [[i.article_id, i.strength, i.last_event_at.isoformat()] for i in items]


def _items_from_json(rows: Sequence[Sequence[Any]]) -> list[ProfileItem]:
    return [ProfileItem(a, float(s), datetime.fromisoformat(t)) for a, s, t in rows]


def state_json(state: ProfileState) -> dict[str, Any]:
    """The `state` column of reco.user_profiles. Lists, not objects, wherever order matters:
    jsonb does not keep the order of object keys."""
    return {
        "weights": [[t, w] for t, w in state.weights.items()],
        "liked": _items_json(state.liked),
        "rejected": _items_json(state.rejected),
        "touched": sorted(state.touched_article_ids),
        "history": [
            [c.event_id, c.type, c.article_id, c.topic_id, c.delta, c.weight_after,
             c.occurred_at.isoformat()]
            for c in state.history
        ],
    }


def state_from_json(doc: Mapping[str, Any]) -> ProfileState:
    return ProfileState(
        weights={int(t): float(w) for t, w in doc["weights"]},
        liked=_items_from_json(doc["liked"]),
        rejected=_items_from_json(doc["rejected"]),
        touched_article_ids=frozenset(doc["touched"]),
        history=[
            WeightChange(
                event_id=e, type=ty, article_id=a, topic_id=int(t), delta=float(d),
                weight_after=float(w), occurred_at=datetime.fromisoformat(at),
            )
            for e, ty, a, t, d, w, at in doc["history"]
        ],
    )


# Profile events of a reader: the same filter for the events and for the mark.
_PROFILE_EVENTS_WHERE = "user_id = %(user)s AND type = ANY(%(types)s) AND article_id IS NOT NULL"


def _event_mark(conn: Connection, user_id: str) -> EventMark:
    count, occurred_at, event_id = conn.execute(
        f"""
        SELECT n.count, l.occurred_at, l.event_id
        FROM (SELECT count(*) FROM app.events WHERE {_PROFILE_EVENTS_WHERE}) n
        LEFT JOIN LATERAL (
          SELECT occurred_at, event_id FROM app.events WHERE {_PROFILE_EVENTS_WHERE}
          ORDER BY occurred_at DESC, event_id DESC LIMIT 1
        ) l ON true
        """,
        {"user": user_id, "types": PROFILE_EVENT_TYPES},
    ).fetchone()
    return EventMark(count=count, last_occurred_at=occurred_at, last_event_id=event_id)


def _events(
    conn: Connection, user_id: str, limit: int, received_after: datetime | None = None
) -> list[Event]:
    """The reader's `limit` most recent profile events (only those received after
    `received_after` if given), oldest first."""
    after = "" if received_after is None else "AND received_at > %(after)s"
    rows = conn.execute(
        f"""
        WITH recent AS (
          SELECT event_id, type, article_id, occurred_at, received_at, payload
          FROM app.events
          WHERE {_PROFILE_EVENTS_WHERE} {after}
          ORDER BY occurred_at DESC, event_id DESC
          LIMIT %(limit)s
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
        {"user": user_id, "types": PROFILE_EVENT_TYPES, "limit": limit, "after": received_after},
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


def _configure(conn: Connection) -> None:
    register_vector(conn)
    conn.commit()  # register_vector reads the catalog; leave the connection idle


def _array(vector: Vector) -> np.ndarray:
    return vector.to_numpy().astype(np.float32, copy=False)


class PgStore:
    """Store backed by a psycopg connection pool, sessions in UTC, pgvector registered.

    The `vector` extension must exist before the pool connects (the backend's migration V1).
    The connecting role needs only the rights of the backend's `swipread_reco` group (V5):
    SELECT on `app`, table creation and read-write on `reco`; nothing is ever written to `app`.
    A method that gets no connection within `timeout_s` raises psycopg_pool.PoolTimeout,
    so an unreachable database fails a request quickly (the backend gives up at 300 ms).
    """

    def __init__(self, dsn: str, timeout_s: float = DEFAULT_POOL_TIMEOUT_S) -> None:
        self._dsn = dsn
        self._timeout_s = timeout_s
        self._pool = ConnectionPool(
            dsn, kwargs={"options": "-c timezone=UTC"}, configure=_configure,
            timeout=timeout_s, open=True,
        )

    def close(self) -> None:
        self._pool.close()

    def ensure_schema(self) -> None:
        """Create the reco tables if missing; SchemaError if schema `reco` or type `vector` is,
        if reco.user_profiles is still the table of the backend's migration V1 (dropped by V8),
        or if a table of `reco` is not readable and writable by this role.

        Runs on its own connection: the pooled ones need the `vector` type to start.
        """
        with psycopg.connect(self._dsn, connect_timeout=max(1, round(self._timeout_s))) as conn:
            # The api and the worker start together: concurrent CREATE TABLE IF NOT EXISTS
            # collide in PostgreSQL (duplicate pg_type entry). One at a time, until commit.
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_KEY,))
            has_reco, has_vector = conn.execute(
                "SELECT to_regnamespace('reco') IS NOT NULL, to_regtype('vector') IS NOT NULL"
            ).fetchone()
            if not has_reco:
                raise SchemaError(
                    "schema reco is missing: it is created by the backend's migration V1"
                    " (Swipread/api), run the backend migrations first"
                )
            if not has_vector:
                raise SchemaError(
                    "type vector is missing: the pgvector extension is created by the backend's"
                    " migration V1 (Swipread/api), run the backend migrations first"
                )
            # pg_attribute rather than information_schema, which hides the columns of a table
            # the role has no rights on.
            legacy = conn.execute(
                "SELECT to_regclass('reco.user_profiles') IS NOT NULL AND NOT EXISTS ("
                " SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('reco.user_profiles')"
                " AND attname = 'state' AND NOT attisdropped)"
            ).fetchone()[0]
            if legacy:
                raise SchemaError(
                    "reco.user_profiles is the backend's old table: its migration V8"
                    " (Swipread/api) drops it, run the backend migrations first"
                )
            no_rights = conn.execute(
                "SELECT string_agg(c.relname, ', ' ORDER BY c.relname) FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'reco' AND c.relkind IN ('r', 'p') AND NOT ("
                " has_table_privilege(c.oid, 'SELECT') AND has_table_privilege(c.oid, 'INSERT')"
                " AND has_table_privilege(c.oid, 'UPDATE')"
                " AND has_table_privilege(c.oid, 'DELETE'))"
            ).fetchone()[0]
            if no_rights:
                # Typically a dump restored with --no-owner: the tables then belong to the
                # restoring role, and the default privileges come back after them.
                raise SchemaError(
                    f"no read-write rights on reco tables {no_rights}: they belong to another"
                    " role, give them back with ALTER TABLE … OWNER TO the reco login role, or"
                    " GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA reco TO"
                    " swipread_reco"
                )
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
            return _events(conn, user_id, limit)

    def events_received_after(
        self, user_id: str, after: datetime | None, limit: int
    ) -> list[Event]:
        """Like events(), restricted to the events received after `after` (all if None)."""
        with self._pool.connection() as conn:
            return _events(conn, user_id, limit, after)

    def event_mark(self, user_id: str) -> EventMark:
        with self._pool.connection() as conn:
            return _event_mark(conn, user_id)

    def profile_inputs(self, user_id: str, limit: int) -> tuple[EventMark, list[Event]]:
        """The mark and the `limit` most recent events, read in one snapshot: the mark never
        covers an event that the events miss."""
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            return _event_mark(conn, user_id), _events(conn, user_id, limit)

    def stored_profile(self, user_id: str) -> StoredProfile | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT basis, state, events_count, last_occurred_at, last_event_id"
                " FROM reco.user_profiles WHERE user_id = %s",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        basis, state, count, occurred_at, event_id = row
        return StoredProfile(
            basis=basis, mark=EventMark(count, occurred_at, event_id), state=state_from_json(state)
        )

    def save_profile(self, user_id: str, profile: StoredProfile, computed_at: datetime) -> None:
        """Insert or replace the reader's row of reco.user_profiles."""
        mark = profile.mark
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO reco.user_profiles (user_id, basis, state, events_count,"
                " last_occurred_at, last_event_id, computed_at) VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (user_id) DO UPDATE SET basis = EXCLUDED.basis,"
                " state = EXCLUDED.state, events_count = EXCLUDED.events_count,"
                " last_occurred_at = EXCLUDED.last_occurred_at,"
                " last_event_id = EXCLUDED.last_event_id, computed_at = EXCLUDED.computed_at",
                (
                    user_id, Jsonb(profile.basis), Jsonb(state_json(profile.state)), mark.count,
                    mark.last_occurred_at, mark.last_event_id, computed_at,
                ),
            )

    def delete_user_data(self, user_id: str) -> None:
        """Delete everything the reco keeps on the reader: profile and lots (contract v1.5)."""
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM reco.user_profiles WHERE user_id = %s", (user_id,))
            conn.execute("DELETE FROM reco.feed_requests WHERE user_id = %s", (user_id,))

    def user_article_ids(self, user_id: str) -> frozenset[str]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT article_id FROM app.user_articles WHERE user_id = %s", (user_id,)
            ).fetchall()
        return frozenset(r[0] for r in rows)

    def embeddings(self, ids: Sequence[str]) -> dict[str, np.ndarray]:
        """EMBEDDING_MODEL embeddings of the given articles; articles gone from app.articles
        or not yet encoded by that model are left out."""
        if not ids:
            return {}
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT e.article_id, e.embedding FROM reco.article_embeddings e"
                " JOIN app.articles a ON a.id = e.article_id"
                " WHERE e.article_id = ANY(%s) AND e.model = %s",
                (list(ids), EMBEDDING_MODEL),
            ).fetchall()
        return {i: _array(emb) for i, emb in rows}

    def last_request_at(self, user_id: str) -> datetime | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT max(generated_at) FROM reco.feed_requests WHERE user_id = %s", (user_id,)
            ).fetchone()
        return row[0]

    def catalog_rows(self, now: datetime, max_age_days: int) -> list[CatalogRow]:
        """Recommendable articles with an EMBEDDING_MODEL embedding (spec 8.2), by id.

        `cluster_id` is always None: the backend has no such column, so same-story detection
        rests on similarity alone (spec 7.2).
        """
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT a.id, p.topic_id, a.source_id, lower(left(a.language, 2)),
                       a.published_at, e.embedding
                {RECOMMENDABLE_FROM}
                JOIN reco.article_embeddings e ON e.article_id = a.id AND e.model = %(model)s
                {RECOMMENDABLE_WHERE}
                ORDER BY a.id
                """,
                {"now": now, "max_age_days": max_age_days, "model": EMBEDDING_MODEL},
            ).fetchall()
        return [
            CatalogRow(
                id=i, topic_id=topic_id, source_id=source_id, language=language,
                cluster_id=None, published_at=published_at, embedding=_array(emb),
            )
            for i, topic_id, source_id, language, published_at, emb in rows
        ]

    def save_lot(
        self, lot: Lot, *, user_id: str, session_id: str | None,
        requested_count: int, generated_at: datetime, params: Params,
    ) -> None:
        """Write the lot's feed_requests row and its feed_items in one transaction (spec 9.1)."""
        with self._pool.connection() as conn, conn.transaction():
            slug = dict(conn.execute("SELECT id, slug FROM app.topics").fetchall())
            conn.execute(
                "INSERT INTO reco.feed_requests (request_id, user_id, session_id, model_version,"
                " requested_count, seed, profile, params, generated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    lot.request_id, user_id, session_id, MODEL_VERSION, requested_count,
                    lot.seed, Jsonb(lot_profile_json(lot, slug)), Jsonb(params.as_dict()), generated_at,
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
        """(id, title, summary) of articles without a `model` row in reco.article_embeddings
        (none, or one of another model), newest ingested first."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT a.id, a.title, a.summary FROM app.articles a"
                " WHERE NOT EXISTS (SELECT 1 FROM reco.article_embeddings e"
                "                   WHERE e.article_id = a.id AND e.model = %s)"
                " ORDER BY a.ingested_at DESC, a.id LIMIT %s",
                (model, limit),
            ).fetchall()
        return [tuple(r) for r in rows]

    def write_embeddings(
        self, rows: Sequence[tuple[str, np.ndarray]], model: str, now: datetime
    ) -> None:
        """Upsert into reco.article_embeddings: one row per article, the latest model wins."""
        if not rows:
            return
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO reco.article_embeddings (article_id, embedding, model, embedded_at)"
                " VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (article_id) DO UPDATE SET embedding = EXCLUDED.embedding,"
                " model = EXCLUDED.model, embedded_at = EXCLUDED.embedded_at",
                [(i, np.asarray(emb, dtype=np.float32), model, now) for i, emb in rows],
            )

    def fallback_ids(
        self, topic_id: int, now: datetime, max_age_days: int, limit: int
    ) -> list[str]:
        """Recommendable articles whose main topic is `topic_id`, newest first (spec 8.5)."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT a.id
                {RECOMMENDABLE_FROM}
                {RECOMMENDABLE_WHERE}
                  AND p.topic_id = %(topic_id)s
                ORDER BY a.published_at DESC, a.id
                LIMIT %(limit)s
                """,
                {"now": now, "max_age_days": max_age_days, "topic_id": topic_id, "limit": limit},
            ).fetchall()
        return [r[0] for r in rows]

    def purge(self, now: datetime, retention_days: int) -> int:
        """Delete requests older than `retention_days` or of users gone from app.users (8.7),
        profiles of users gone and embeddings of articles gone from app.articles; the number of
        requests deleted."""
        with self._pool.connection() as conn, conn.transaction():
            cur = conn.execute(
                "DELETE FROM reco.feed_requests r"
                " WHERE r.generated_at < %s - make_interval(days => %s)"
                " OR NOT EXISTS (SELECT 1 FROM app.users u WHERE u.id = r.user_id)",
                (now, retention_days),
            )
            conn.execute(
                "DELETE FROM reco.user_profiles p"
                " WHERE NOT EXISTS (SELECT 1 FROM app.users u WHERE u.id = p.user_id)"
            )
            conn.execute(
                "DELETE FROM reco.article_embeddings e"
                " WHERE NOT EXISTS (SELECT 1 FROM app.articles a WHERE a.id = e.article_id)"
            )
            return cur.rowcount
