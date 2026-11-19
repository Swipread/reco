"""PgStore against a real PostgreSQL (fixtures in conftest.py)."""

import dataclasses
from datetime import timedelta

import numpy as np
import psycopg
import pytest
from psycopg.types.json import Jsonb

from swipread_reco.models import Lot, LotItem, Profile, ProfileItem
from swipread_reco.params import EMBEDDING_MODEL, MODEL_VERSION, Params
from swipread_reco.store import PgStore, SchemaError

from .helpers import NOW, unit_vector

pytestmark = pytest.mark.integration


def insert_event(
    conn: psycopg.Connection, event_id: str, type_: str, article_id: str | None,
    hours_ago: float, user_id: str = "u_1", payload: dict | None = None,
) -> None:
    occurred = NOW - timedelta(hours=hours_ago)
    conn.execute(
        "INSERT INTO app.events (event_id, user_id, session_id, type, article_id, payload,"
        " occurred_at, received_at) VALUES (%s, %s, 's_1', %s, %s, %s, %s, %s)",
        (event_id, user_id, type_, article_id, Jsonb(payload or {}), occurred,
         occurred + timedelta(seconds=2)),
    )


def make_lot(request_id: str, article_ids: list[str]) -> Lot:
    liked = ProfileItem(article_id="a_ok", strength=0.5, last_event_at=NOW)
    profile = Profile(
        weights={1: 0.7, 2: 0.2, 3: 0.1}, liked=[liked], rejected=[], echo_topic=2,
        touched_article_ids=frozenset({"a_ok"}), history=[],
    )
    items = [
        LotItem(
            article_id=aid, topic_id=1, reason="similar", score=0.5 + 0.1 * k,
            explanation={
                "affinity": 0.4, "freshness": 0.9, "penalty": 0.0, "topicWeight": 0.7,
                "nearestLiked": [{"articleId": "a_ok", "sim": 0.8}],
                "nearestRejected": None, "relaxedSource": False,
            },
        )
        for k, aid in enumerate(article_ids)
    ]
    return Lot(
        request_id=request_id, seed=2**62 + 7, items=items, profile=profile,
        shares={1: 0.6, 2: 0.25, 3: 0.15}, mu=0.12,
    )


def save(store: PgStore, request_id: str, user_id: str, generated_at, ids=("a_ok",)) -> None:
    store.save_lot(
        make_lot(request_id, list(ids)), user_id=user_id, session_id="s_1",
        requested_count=10, generated_at=generated_at, params=Params(),
    )


def test_ensure_schema_creates_the_reco_tables_as_the_restricted_role(store, conn):
    store.ensure_schema()
    store.ensure_schema()
    owned = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'reco' AND tableowner = 'reco_test'"
        " ORDER BY 1"
    ).fetchall()
    assert [t for (t,) in owned] == ["article_embeddings", "feed_items", "feed_requests"]


def test_ensure_schema_fails_clearly_without_schema_reco(reco_dsn, conn):
    conn.execute("DROP SCHEMA reco CASCADE")
    s = PgStore(reco_dsn)
    try:
        with pytest.raises(SchemaError, match="schema reco is missing.*migration"):
            s.ensure_schema()
    finally:
        s.close()


def test_the_store_role_cannot_write_app_nor_read_private_columns(store, reco_dsn):
    with psycopg.connect(reco_dsn) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("SELECT email FROM app.users")
    with psycopg.connect(reco_dsn) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("UPDATE app.articles SET title = title")


def test_user_locale_and_topics(store, seed):
    assert store.user_locale("u_1") == "fr-FR" and store.user_locale("u_nope") is None
    assert store.chosen_topics("u_1") == {1} and [t.slug for t in store.topics()] == [
        "tech", "eco", "sport"
    ]
    assert store.chosen_topics("u_nope") == frozenset()


def test_user_article_ids_and_titles(store, seed):
    assert store.user_article_ids("u_1") == {"a_ok", "a_old"}
    assert store.user_article_ids("u_nope") == frozenset()
    assert store.article_titles(["a_ok", "a_nope"]) == {"a_ok": "Title a_ok"}


def test_catalog_rows_apply_every_filter(store, seed):
    rows = store.catalog_rows(NOW, 14)
    assert [r.id for r in rows] == ["a_ok"]
    row = rows[0]
    assert (row.source_id, row.language, row.cluster_id) == (1, "fr", None)
    assert row.published_at == NOW - timedelta(days=1)
    assert row.embedding.dtype == np.float32
    np.testing.assert_allclose(row.embedding, unit_vector(0), rtol=1e-6)


def test_primary_topic_is_highest_confidence_then_lowest_id(store, seed, conn):
    # a_ok has (topic 2, 0.9), (topic 1, 0.9), (topic 3, 0.5) -> topic 1
    assert store.catalog_rows(NOW, 14)[0].topic_id == 1
    insert_event(conn, "e_1", "open", "a_ok", 1)
    assert store.events("u_1", 10)[0].topic_id == 1
    assert store.fallback_ids(2, NOW, 14, 10) == []


def test_events_are_limited_to_most_recent_and_carry_topic(store, seed, conn):
    insert_event(conn, "e_1", "view", "a_ok", 5, payload={"dwellMs": 4200})
    insert_event(conn, "e_2", "open", "a_ok", 4)
    insert_event(conn, "e_3", "read", "a_ok", 3, payload={"readMs": 9000, "scrollDepth": 0.9})
    insert_event(conn, "e_4", "like", "a_ok", 2)
    insert_event(conn, "e_5", "hide", "a_gone", 1, payload={"reason": "boring"})
    insert_event(conn, "e_6", "open", None, 0.5)    # article_id is nullable: never returned
    events = store.events("u_1", 3)
    assert sorted(e.event_id for e in events) == ["e_3", "e_4", "e_5"]
    by_id = {e.event_id: e for e in events}
    read = by_id["e_3"]
    assert (read.type, read.article_id, read.topic_id, read.word_count) == ("read", "a_ok", 1, 100)
    assert read.payload == {"readMs": 9000, "scrollDepth": 0.9}
    assert read.occurred_at == NOW - timedelta(hours=3)
    assert read.received_at == NOW - timedelta(hours=3) + timedelta(seconds=2)
    gone = by_id["e_5"]
    assert (gone.topic_id, gone.word_count) == (None, None)


def test_impressions_are_not_returned_unsave_is(store, seed, conn):
    insert_event(conn, "e_1", "impression", "a_ok", 2)
    insert_event(conn, "e_2", "view", "a_ok", 1, payload={"dwellMs": 300})
    insert_event(conn, "e_3", "like", "a_ok", 1, user_id="u_other")
    insert_event(conn, "e_4", "unsave", "a_ok", 0.5)
    insert_event(conn, "e_5", "read", "a_ok", 0.4, payload={"readMs": 9000})
    assert [(e.event_id, e.type) for e in store.events("u_1", 10)] == [
        ("e_2", "view"), ("e_4", "unsave"), ("e_5", "read")
    ]


def test_embeddings_roundtrip_and_skip_missing(store, seed, conn):
    conn.execute(
        "INSERT INTO reco.article_embeddings VALUES ('a_nope', %s, %s, %s)",
        (unit_vector(9), EMBEDDING_MODEL, NOW),
    )
    # a_noemb has only app.articles.embedding, a_othermodel another model, a_nope no article.
    found = store.embeddings(["a_ok", "a_noemb", "a_othermodel", "a_nope"])
    assert set(found) == {"a_ok"}
    assert found["a_ok"].dtype == np.float32
    np.testing.assert_allclose(found["a_ok"], unit_vector(0), rtol=1e-6)
    assert store.embeddings([]) == {}


def test_save_lot_then_request_and_last_request_at(store, seed):
    assert store.last_request_at("u_1") is None
    assert store.request("r_nope") is None
    generated_at = NOW - timedelta(minutes=5)
    save(store, "r_1", "u_1", generated_at, ids=("a_old", "a_ok", "a_gone"))

    req = store.request("r_1")
    assert req is not None
    assert (req["request_id"], req["user_id"], req["session_id"]) == ("r_1", "u_1", "s_1")
    assert (req["model_version"], req["requested_count"]) == (MODEL_VERSION, 10)
    assert req["seed"] == 2**62 + 7
    assert req["generated_at"] == generated_at
    assert req["params"] == Params().as_dict()
    assert req["profile"] == {
        "weights": {"tech": 0.7, "eco": 0.2, "sport": 0.1},
        "shares": {"tech": 0.6, "eco": 0.25, "sport": 0.15},
        "likedCount": 1, "rejectedCount": 0, "echoTopic": "eco", "mu": 0.12,
    }
    items = req["items"]
    assert [(i["position"], i["article_id"]) for i in items] == [
        (0, "a_old"), (1, "a_ok"), (2, "a_gone")
    ]
    assert [i["title"] for i in items] == ["Title a_old", "Title a_ok", None]
    first = items[0]
    assert (first["topic_id"], first["reason"]) == (1, "similar")
    assert first["score"] == pytest.approx(0.5)
    assert first["explanation"]["nearestLiked"] == [{"articleId": "a_ok", "sim": 0.8}]
    assert first["explanation"]["nearestRejected"] is None

    assert store.last_request_at("u_1") == generated_at
    save(store, "r_2", "u_1", NOW)
    assert store.last_request_at("u_1") == NOW


def test_save_lot_is_one_transaction(store, seed):
    lot = make_lot("r_bad", ["a_ok", "a_old"])
    broken = dataclasses.replace(lot.items[1], topic_id=None)  # violates feed_items NOT NULL
    lot = dataclasses.replace(lot, items=[lot.items[0], broken])
    with pytest.raises(psycopg.errors.NotNullViolation):
        store.save_lot(
            lot, user_id="u_1", session_id=None, requested_count=2,
            generated_at=NOW, params=Params(),
        )
    assert store.request("r_bad") is None
    assert store.last_request_at("u_1") is None


def test_articles_to_embed_and_write(store, seed, conn):
    # Without an embedding (a_noemb) or with one of another model (a_othermodel), newest first.
    assert store.articles_to_embed(EMBEDDING_MODEL, 10) == [
        ("a_noemb", "Title a_noemb", "Summary a_noemb"),
        ("a_othermodel", "Title a_othermodel", "Summary a_othermodel"),
    ]
    # Another model: every article, most recently ingested first.
    assert [a[0] for a in store.articles_to_embed("other-model", 3)] == [
        "a_notopic", "a_inactive", "a_old"
    ]
    app_before = conn.execute("SELECT id, embedding FROM app.articles ORDER BY id").fetchall()

    later = NOW + timedelta(hours=1)
    store.write_embeddings(
        [("a_noemb", unit_vector(42)), ("a_othermodel", unit_vector(43))], EMBEDDING_MODEL, later
    )

    assert store.articles_to_embed(EMBEDDING_MODEL, 10) == []
    np.testing.assert_allclose(
        store.embeddings(["a_noemb"])["a_noemb"], unit_vector(42), rtol=1e-6
    )
    rows = conn.execute(
        "SELECT article_id, model, embedded_at FROM reco.article_embeddings"
        " WHERE article_id IN ('a_noemb', 'a_othermodel') ORDER BY article_id"
    ).fetchall()
    assert rows == [("a_noemb", EMBEDDING_MODEL, later), ("a_othermodel", EMBEDDING_MODEL, later)]
    # The upsert replaced a_othermodel's old-model row; app.articles is untouched.
    assert [r.id for r in store.catalog_rows(NOW, 14)] == ["a_noemb", "a_ok", "a_othermodel"]
    assert conn.execute("SELECT id, embedding FROM app.articles ORDER BY id").fetchall() == app_before


def test_fallback_ids_newest_first(store, seed):
    # Topic 1, recent, live source, encoded or not: a_noemb (1 h old), a_ok and a_othermodel
    # (1 day old, by id).
    assert store.fallback_ids(1, NOW, 14, 10) == ["a_noemb", "a_ok", "a_othermodel"]
    assert store.fallback_ids(1, NOW, 14, 1) == ["a_noemb"]
    assert store.fallback_ids(3, NOW, 14, 10) == []


def test_purge_removes_old_and_orphans(store, seed, conn):
    save(store, "r_old", "u_1", NOW - timedelta(days=31))
    save(store, "r_orphan", "u_gone", NOW - timedelta(days=1))
    save(store, "r_new", "u_1", NOW - timedelta(days=1))
    assert store.purge(NOW, 30) == 2
    assert store.request("r_old") is None and store.request("r_orphan") is None
    assert store.request("r_new") is not None
    (items,) = conn.execute("SELECT count(*) FROM reco.feed_items").fetchone()
    assert items == 1


def test_purge_removes_embeddings_of_deleted_articles(store, seed, conn):
    conn.execute("DELETE FROM app.articles WHERE id IN ('a_notopic', 'a_inactive')")
    store.purge(NOW, 30)
    kept = conn.execute("SELECT article_id FROM reco.article_embeddings ORDER BY 1").fetchall()
    assert [k for (k,) in kept] == ["a_ok", "a_old", "a_othermodel"]
