"""Consumer of the `events` stream (contract v1.5, spec 8.6) over fakeredis and the FakeStore."""

import json
from datetime import timedelta

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from swipread_reco.params import Params
from swipread_reco.worker.stream import GROUP, STREAM, StreamConsumer, parse_message

from ..fakes import NOW, FakeStore, StoreDown


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def redis() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def consumer(store, redis) -> StreamConsumer:
    return StreamConsumer(redis, store, Params(), lambda: NOW)


def publish(redis, user_id: str, type_: str, event_id: str = "e_x") -> None:
    """The backend's encoding: one field `data` holding the message JSON."""
    redis.xadd(STREAM, {"data": json.dumps({
        "version": 1, "eventId": event_id, "userId": user_id, "type": type_,
        "occurredAt": "2026-11-10T11:59:00Z", "receivedAt": "2026-11-10T11:59:01Z", "payload": {},
    })})


class DownRedis:
    """Every command fails as with an unreachable server."""

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise RedisConnectionError("Connection refused")

        return fail


def pending(redis) -> int:
    return redis.xpending(STREAM, GROUP)["pending"]


def like(store: FakeStore, k: int = 0) -> str:
    article = store.sim.catalog.ids[k]
    store.add_event("u_1", "like", article, occurred_at=NOW - timedelta(minutes=10 - k))
    return article


def test_a_profile_event_refreshes_the_profile_from_app_events(store, redis, consumer):
    article = like(store)
    publish(redis, "u_1", "like")
    assert consumer.poll(0.01) == 1
    _basis, mark, state, computed_at = store.profiles["u_1"]
    assert mark.count == 1 and article in state["touched"] and computed_at == NOW
    assert pending(redis) == 0


def test_a_batch_refreshes_each_reader_once(store, redis, consumer):
    like(store)
    for k in range(5):
        publish(redis, "u_1", "view", f"e_{k}")
    publish(redis, "u_new", "open")
    assert consumer.poll(0.01) == 6
    assert store.profile_saves == 2 and set(store.profiles) == {"u_1", "u_new"}


def test_a_duplicate_or_late_message_gives_the_same_profile(store, redis, consumer):
    like(store, 0)
    like(store, 1)
    publish(redis, "u_1", "like", "e_1")
    consumer.poll(0.01)
    first = store.profiles["u_1"][2]
    publish(redis, "u_1", "like", "e_0")    # older event, delivered after
    publish(redis, "u_1", "like", "e_1")    # duplicate
    consumer.poll(0.01)
    assert store.profiles["u_1"][2] == first and store.profile_saves == 2


def test_impressions_and_unknown_types_are_acknowledged_without_work(store, redis, consumer):
    publish(redis, "u_1", "impression")
    publish(redis, "u_1", "something_new")
    assert consumer.poll(0.01) == 2
    assert store.profile_saves == 0 and pending(redis) == 0


def test_malformed_messages_are_skipped_and_acknowledged(store, redis, consumer, caplog):
    redis.xadd(STREAM, {"data": "not json"})
    redis.xadd(STREAM, {"other": "field"})
    redis.xadd(STREAM, {"data": json.dumps({"type": "like"})})   # no userId
    assert consumer.poll(0.01) == 3
    assert pending(redis) == 0 and "malformed" in caplog.text


@pytest.mark.parametrize("fields,ok", [
    ({"data": json.dumps({"userId": "u_1", "type": "like"})}, True),
    ({"data": json.dumps({"userId": 1, "type": "like"})}, False),
    ({"data": json.dumps(["u_1"])}, False),
    ({"data": json.dumps({"userId": "u_1"})}, False),
])
def test_parse_message(fields, ok):
    assert (parse_message(fields) is not None) == ok


def test_user_deleted_removes_profile_and_lots_at_once(store, redis, consumer):
    like(store)
    publish(redis, "u_1", "like")
    consumer.poll(0.01)
    store.lots["r_1"] = {"user_id": "u_1", "generated_at": NOW}
    store.lots["r_2"] = {"user_id": "u_new", "generated_at": NOW}
    publish(redis, "u_1", "user_deleted")
    consumer.poll(0.01)
    assert "u_1" not in store.profiles and set(store.lots) == {"r_2"}
    assert pending(redis) == 0


def test_an_event_after_user_deleted_in_the_same_batch_stores_nothing(store, redis, consumer):
    like(store)
    publish(redis, "u_1", "user_deleted")
    publish(redis, "u_1", "like")
    consumer.poll(0.01)
    assert "u_1" not in store.profiles and store.profile_saves == 0


def test_a_reader_gone_from_app_users_keeps_nothing(store, redis, consumer):
    store.profiles["u_gone"] = ({}, None, {}, NOW)
    publish(redis, "u_gone", "like")
    consumer.poll(0.01)
    assert "u_gone" not in store.profiles and pending(redis) == 0


def test_a_failed_refresh_is_acknowledged_the_request_will_recompute(store, redis, consumer, caplog):
    store.fail = True
    publish(redis, "u_1", "like")
    consumer.poll(0.01)
    assert pending(redis) == 0 and "not refreshed" in caplog.text


def test_a_failed_deletion_stays_pending_and_is_retried(store, redis, consumer, monkeypatch):
    store.profiles["u_1"] = ({}, None, {}, NOW)
    publish(redis, "u_1", "user_deleted")
    publish(redis, "u_new", "impression")
    original = store.delete_user_data

    def down(user_id):
        raise StoreDown("store is down")

    monkeypatch.setattr(store, "delete_user_data", down)
    consumer.poll(0.01)
    assert pending(redis) == 1
    monkeypatch.setattr(store, "delete_user_data", original)
    assert consumer.retry_pending() == 1
    assert pending(redis) == 0 and "u_1" not in store.profiles


def test_the_group_starts_at_the_beginning_of_the_stream(store, redis, consumer):
    like(store)
    publish(redis, "u_1", "like")          # published before the reco's first run
    assert consumer.poll(0.01) == 1 and "u_1" in store.profiles


def test_an_existing_group_is_kept(store, redis):
    publish(redis, "u_1", "like", "e_old")
    redis.xgroup_create(STREAM, GROUP, id="$")
    publish(redis, "u_1", "like", "e_new")
    assert StreamConsumer(redis, store, Params(), lambda: NOW).poll(0.01) == 1


def test_a_flushed_redis_gets_its_group_back(store, redis, consumer):
    consumer.poll(0.01)
    redis.flushall()
    assert consumer.poll(0.01) == 0         # NOGROUP: logged, group recreated next time
    like(store)
    publish(redis, "u_1", "like")
    assert consumer.poll(0.01) == 1


def test_an_unreachable_redis_is_logged_not_raised(store, caplog):
    consumer = StreamConsumer(DownRedis(), store, Params(), lambda: NOW)
    assert consumer.poll(0.01) == 0 and consumer.retry_pending() == 0
    assert "unreadable" in caplog.text


def test_the_consumer_takes_back_its_own_unacknowledged_messages(store, redis):
    publish(redis, "u_1", "like")
    redis.xgroup_create(STREAM, GROUP, id="0")
    redis.xreadgroup(GROUP, "worker", {STREAM: ">"})     # read, then the worker crashed
    like(store)
    assert StreamConsumer(redis, store, Params(), lambda: NOW).retry_pending() == 1
    assert pending(redis) == 0 and "u_1" in store.profiles

