"""reco.user_profiles and the `events` stream against a real PostgreSQL (contract v1.5)."""

import json
from datetime import timedelta

import fakeredis
import pytest

from swipread_reco.models import EventMark, StoredProfile
from swipread_reco.params import Params
from swipread_reco.profile import replay_profile
from swipread_reco.profiles import current_profile, profile_basis
from swipread_reco.store import PgStore, SchemaError
from swipread_reco.worker.stream import GROUP, STREAM, StreamConsumer

from .helpers import NOW
from .test_store import insert_event, save

pytestmark = pytest.mark.integration

TOPICS = [1, 2, 3]


def stored(store: PgStore, user_id: str = "u_1") -> StoredProfile:
    profile = store.stored_profile(user_id)
    assert profile is not None
    return profile


def test_event_mark_counts_profile_events_and_names_the_last(store, seed, conn):
    assert store.event_mark("u_1") == EventMark(0, None, None)
    insert_event(conn, "e_1", "like", "a_ok", 3)
    insert_event(conn, "e_3", "open", "a_old", 1)
    insert_event(conn, "e_2", "open", "a_ok", 1)          # same time: e_3 is the last
    insert_event(conn, "e_4", "impression", "a_ok", 0.5)  # not a profile event
    insert_event(conn, "e_5", "open", None, 0.5)          # no article
    insert_event(conn, "e_6", "like", "a_ok", 0.1, user_id="u_other")
    assert store.event_mark("u_1") == EventMark(3, NOW - timedelta(hours=1), "e_3")


def test_events_received_after(store, seed, conn):
    insert_event(conn, "e_1", "like", "a_ok", 3)
    insert_event(conn, "e_2", "open", "a_old", 1)
    after = NOW - timedelta(hours=2)
    assert [e.event_id for e in store.events_received_after("u_1", after, 10)] == ["e_2"]
    assert [e.event_id for e in store.events_received_after("u_1", None, 10)] == ["e_1", "e_2"]


def test_profile_roundtrip_keeps_every_value_and_order(store, seed, conn):
    for k in range(12):
        insert_event(conn, f"e_{k}", "like" if k % 3 else "hide", "a_ok" if k % 2 else "a_old", 12 - k)
    mark, events = store.profile_inputs("u_1", 5000)
    assert mark.count == 12
    state = replay_profile(events, [3, 1, 2], frozenset({1}), Params())
    profile = StoredProfile(profile_basis([3, 1, 2], {1}, Params()), mark, state)
    store.save_profile("u_1", profile, NOW)
    assert stored(store) == profile
    assert list(stored(store).state.weights) == [3, 1, 2]   # jsonb does not reorder them

    store.save_profile("u_1", StoredProfile({"v": 2}, EventMark(0, None, None), state), NOW)
    assert stored(store).basis == {"v": 2}                  # replaced, not added
    (rows,) = conn.execute("SELECT count(*) FROM reco.user_profiles").fetchone()
    assert rows == 1


def test_current_profile_recomputes_after_a_new_event_only(store, seed, conn):
    insert_event(conn, "e_1", "like", "a_ok", 3)
    first = current_profile(store, "u_1", TOPICS, frozenset({1}), Params(), NOW)
    assert stored(store).mark.count == 1
    conn.execute("UPDATE reco.user_profiles SET computed_at = %s", (NOW - timedelta(days=1),))

    assert current_profile(store, "u_1", TOPICS, frozenset({1}), Params(), NOW) == first
    (at,) = conn.execute("SELECT computed_at FROM reco.user_profiles").fetchone()
    assert at == NOW - timedelta(days=1)                    # fresh: not written again

    insert_event(conn, "e_2", "hide", "a_old", 1)
    second = current_profile(store, "u_1", TOPICS, frozenset({1}), Params(), NOW)
    assert "a_old" in second.touched_article_ids
    assert stored(store).mark == EventMark(2, NOW - timedelta(hours=1), "e_2")


def test_delete_user_data_removes_profile_and_lots_of_that_user_only(store, seed, conn):
    insert_event(conn, "e_1", "like", "a_ok", 3)
    current_profile(store, "u_1", TOPICS, frozenset({1}), Params(), NOW)
    store.save_profile("u_other", stored(store), NOW)
    save(store, "r_1", "u_1", NOW)
    save(store, "r_2", "u_other", NOW)
    store.delete_user_data("u_1")
    assert store.stored_profile("u_1") is None and store.request("r_1") is None
    assert store.stored_profile("u_other") is not None and store.request("r_2") is not None
    (items,) = conn.execute("SELECT count(*) FROM reco.feed_items").fetchone()
    assert items == 1


def test_purge_removes_profiles_of_users_gone(store, seed, conn):
    insert_event(conn, "e_1", "like", "a_ok", 3)
    current_profile(store, "u_1", TOPICS, frozenset({1}), Params(), NOW)
    store.save_profile("u_gone", stored(store), NOW)
    store.purge(NOW, 30)
    assert store.stored_profile("u_gone") is None
    assert store.stored_profile("u_1") is not None


def test_ensure_schema_refuses_the_backends_old_user_profiles(reco_dsn, conn):
    conn.execute("DROP TABLE IF EXISTS reco.user_profiles")
    conn.execute(
        "CREATE TABLE reco.user_profiles (user_id text PRIMARY KEY, events_count int NOT NULL)"
    )
    s = PgStore(reco_dsn)
    try:
        with pytest.raises(SchemaError, match="old table.*V8"):
            s.ensure_schema()
    finally:
        s.close()


def test_ensure_schema_names_the_reco_tables_it_has_no_rights_on(store, conn):
    # As after a restore with --no-owner: owned by another role, no grant to the group.
    conn.execute("ALTER TABLE reco.feed_items OWNER TO postgres")
    conn.execute("REVOKE ALL ON reco.feed_items FROM swipread_reco")
    with pytest.raises(SchemaError, match="no read-write rights on reco tables feed_items:"):
        store.ensure_schema()


def test_stream_consumer_builds_and_deletes_profiles_in_the_database(store, seed, conn):
    redis = fakeredis.FakeRedis(decode_responses=True)
    consumer = StreamConsumer(redis, store, Params(), lambda: NOW)
    insert_event(conn, "e_1", "like", "a_ok", 3)
    redis.xadd(STREAM, {"data": json.dumps({"eventId": "e_1", "userId": "u_1", "type": "like"})})
    assert consumer.poll(0.01) == 1
    assert stored(store).mark.count == 1
    assert redis.xpending(STREAM, GROUP)["pending"] == 0

    save(store, "r_1", "u_1", NOW)
    redis.xadd(STREAM, {"data": json.dumps({"eventId": "e_9", "userId": "u_1", "type": "user_deleted"})})
    assert consumer.poll(0.01) == 1
    assert store.stored_profile("u_1") is None and store.request("r_1") is None

