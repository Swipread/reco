"""Fixtures for the PostgreSQL integration tests, run against the database at RECO_TEST_DSN.

The `store` fixture drops and recreates the `app` and `reco` schemas of that database:
point RECO_TEST_DSN only at a disposable database. RECO_TEST_DSN must be a superuser: it
seeds the data and creates the login role `reco_test`, member of the backend's group role
`swipread_reco`, that the PgStore under test connects as.
"""

import os
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from pgvector.psycopg import register_vector
from psycopg.conninfo import make_conninfo

from swipread_reco.params import EMBEDDING_MODEL
from swipread_reco.store import PgStore

from .helpers import NOW, unit_vector

APP_SCHEMA = Path(__file__).resolve().parents[2] / "sql" / "app_schema.sql"
RECO_ROLE = "reco_test"
RECO_PASSWORD = "reco_test"


def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("integration") and not os.environ.get("RECO_TEST_DSN"):
        pytest.skip("RECO_TEST_DSN is not set")


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    """Superuser autocommit connection to the test database, for setup and direct checks."""
    with psycopg.connect(os.environ["RECO_TEST_DSN"], autocommit=True) as c:
        yield c


@pytest.fixture
def reco_dsn(conn: psycopg.Connection) -> str:
    """DSN of the restricted login role, on freshly recreated `app` and `reco` schemas."""
    conn.execute("DROP SCHEMA IF EXISTS reco CASCADE")
    conn.execute("DROP SCHEMA IF EXISTS app CASCADE")
    conn.execute(APP_SCHEMA.read_text())
    register_vector(conn)
    conn.execute(
        f"""
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RECO_ROLE}') THEN
            CREATE ROLE {RECO_ROLE} LOGIN PASSWORD '{RECO_PASSWORD}' IN ROLE swipread_reco;
          END IF;
        END
        $$
        """
    )
    return make_conninfo(os.environ["RECO_TEST_DSN"], user=RECO_ROLE, password=RECO_PASSWORD)


@pytest.fixture
def store(reco_dsn: str) -> Iterator[PgStore]:
    """PgStore connected as the restricted role, after its ensure_schema()."""
    s = PgStore(reco_dsn)
    s.ensure_schema()
    try:
        yield s
    finally:
        s.close()


# id, source, published (days before NOW), embedding model (None: not encoded), topics
# (topic_id, confidence), ingested (hours before NOW). Each article but a_ok fails exactly
# one catalog filter.
ARTICLES = [
    ("a_ok", 1, 1, EMBEDDING_MODEL, [(2, 0.9), (1, 0.9), (3, 0.5)], 6),
    ("a_othermodel", 1, 1, "old-model", [(1, 0.8)], 5),
    ("a_noemb", 1, 1 / 24, None, [(1, 0.8)], 4),
    ("a_old", 1, 20, EMBEDDING_MODEL, [(1, 0.8)], 3),
    ("a_inactive", 2, 1, EMBEDDING_MODEL, [(1, 0.8)], 2),
    ("a_notopic", 1, 1, EMBEDDING_MODEL, [], 1),
]


@pytest.fixture
def seed(store: PgStore, conn: psycopg.Connection) -> None:
    """3 topics, 2 sources (one inactive), user u_1 with topic 1, and the ARTICLES.

    Embeddings go to reco.article_embeddings. app.articles.embedding, which the reco never
    reads, is filled for a_noemb only, so that reading it by mistake shows.
    """
    conn.execute(
        "INSERT INTO app.topics (id, slug, label) VALUES"
        " (1, 'tech', 'Tech'), (2, 'eco', 'Économie'), (3, 'sport', 'Sport')"
    )
    conn.execute(
        "INSERT INTO app.sources (id, name, site_url, feed_url, language, active) VALUES"
        " (1, 'Live', 'https://live.example', 'https://live.example/rss', 'fr', true),"
        " (2, 'Dead', 'https://dead.example', 'https://dead.example/rss', 'fr', false)"
    )
    conn.execute(
        "INSERT INTO app.users (id, email, password_hash, display_name)"
        " VALUES ('u_1', 'u1@example.com', 'x', 'User One')"
    )
    conn.execute("INSERT INTO app.user_topics (user_id, topic_id) VALUES ('u_1', 1)")
    for k, (aid, source, age_days, model, topics, ingested_h) in enumerate(ARTICLES):
        conn.execute(
            "INSERT INTO app.articles (id, source_id, url, title, summary, language, word_count,"
            " published_at, ingested_at, embedding)"
            " VALUES (%s, %s, %s, %s, %s, 'FR-fr', %s, %s, %s, %s)",
            (
                aid, source, f"https://live.example/{aid}", f"Title {aid}", f"Summary {aid}",
                100 * (k + 1), NOW - timedelta(days=age_days), NOW - timedelta(hours=ingested_h),
                unit_vector(100 + k) if model is None else None,
            ),
        )
        if model is not None:
            conn.execute(
                "INSERT INTO reco.article_embeddings (article_id, embedding, model, embedded_at)"
                " VALUES (%s, %s, %s, %s)",
                (aid, unit_vector(k), model, NOW - timedelta(hours=ingested_h)),
            )
        for topic_id, confidence in topics:
            conn.execute(
                "INSERT INTO app.article_topics (article_id, topic_id, confidence)"
                " VALUES (%s, %s, %s)",
                (aid, topic_id, confidence),
            )
    conn.execute(
        "INSERT INTO app.user_articles (user_id, article_id, first_seen_at)"
        " VALUES ('u_1', 'a_ok', %s), ('u_1', 'a_old', %s)",
        (NOW, NOW),
    )
