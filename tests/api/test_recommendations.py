"""POST /v1/recommendations and GET /healthz over the in-memory store (spec 7.1, 8.2, 8.3)."""

import re
import time
from datetime import timedelta

import fakeredis
import pytest
from fastapi.testclient import TestClient

from swipread_reco.api import RedisSeen, create_app
from swipread_reco.params import Params
from swipread_reco.store import PgStore

from ..fakes import NOW


def post(client, **body):
    return client.post("/v1/recommendations", json={"userId": "u_1", "count": 50, **body})


def test_contract_shape(client):
    r = post(client, context={"sessionId": "s_1", "locale": "fr-FR", "device": "mobile"})
    b = r.json()
    assert r.status_code == 200 and set(b) == {"requestId", "modelVersion", "generatedAt", "items"}
    assert b["modelVersion"] == "topic-weights-v1" and re.fullmatch(r"r_[0-9a-f]{32}", b["requestId"])
    assert len(b["items"]) == 50 and set(b["items"][0]) == {"articleId", "score", "reason"}
    assert all(
        0 <= i["score"] <= 1 and i["reason"] in {"similar", "onboarding", "explore"}
        for i in b["items"]
    )


def test_generated_at_and_rounded_scores(client):
    b = post(client).json()
    assert b["generatedAt"] == "2026-11-10T12:00:00Z"
    assert all(round(i["score"], 4) == i["score"] for i in b["items"])


def test_count_is_respected(client):
    assert len(post(client, count=7).json()["items"]) == 7


@pytest.mark.parametrize("body", [{"count": 0}, {"count": 101}, {"count": "x"}, {"userId": None}])
def test_invalid_request_is_400(client, body):
    assert post(client, **body).status_code == 400


def test_missing_body_is_400(client):
    assert client.post("/v1/recommendations", content=b"not json").status_code == 400


def test_unknown_user_is_404(client):
    assert post(client, userId="u_nope").status_code == 404


def test_store_failure_is_503(client, store):
    store.fail = True
    assert post(client).status_code == 503


def test_store_failure_is_logged_with_its_traceback(client, store, caplog):
    store.fail = True
    post(client)
    record = next(r for r in caplog.records if r.getMessage().startswith("store call"))
    assert record.exc_info is not None and "StoreDown" in caplog.text


def test_unreachable_database_is_503_without_waiting(store, seen, cache):
    pg = PgStore("postgresql://postgres@127.0.0.1:1/postgres", timeout_s=0.2)
    try:
        client = TestClient(create_app(pg, seen, cache, Params(), clock=lambda: NOW))
        start = time.monotonic()
        assert post(client).status_code == 503
        assert time.monotonic() - start < 1.0
    finally:
        pg.close()


def test_new_user_with_topics_gets_a_lot(client):
    assert len(post(client, userId="u_new").json()["items"]) == 50


def test_events_are_read_up_to_events_max(client, store):
    post(client)
    assert store.events_limit == Params().events_max


def test_exclusions_come_from_request_redis_and_user_articles(client, store, seen):
    a, b, c = store.sim.ids(1)[:3]
    seen.add("u_1", b)
    store.user_articles["u_1"] = {c}
    ids = {i["articleId"] for i in post(client, exclude=[a]).json()["items"]}
    assert not ids & {a, b, c}


def test_redis_failure_is_tolerated(client, seen, caplog):       # Review Focus 5
    seen.fail = True
    assert len(post(client).json()["items"]) == 50 and "seen" in caplog.text


def test_locale_falls_back_to_user_then_filters(client, store):
    store.locales["u_1"] = "de-DE"
    assert post(client).json()["items"] == []                    # Review Focus 3
    assert len(post(client, context={"locale": "fr-FR"}).json()["items"]) == 50


def test_lot_is_saved_with_request_fields(client, store):
    b = post(client, context={"sessionId": "s_9"}).json()
    saved = store.lots[b["requestId"]]
    assert saved["user_id"] == "u_1" and saved["session_id"] == "s_9"
    assert saved["requested_count"] == 50 and saved["generated_at"] == NOW
    assert [i.article_id for i in saved["lot"].items] == [i["articleId"] for i in b["items"]]


def test_echo_uses_previous_request_time(client, store):
    post(client)                                                  # fixe last_request_at
    store.add_event("u_1", "like", store.sim.ids(4)[0], received_at=NOW + timedelta(seconds=5))
    first = post(client).json()["items"][0]["articleId"]
    assert store.sim.subtopic[first][0] == 4


def test_no_echo_when_exclude_holds_the_liked_topic(client, store):    # spec 4.4, last rule
    post(client)                                                  # fixe last_request_at
    store.add_event("u_1", "like", store.sim.ids(4)[0], received_at=NOW + timedelta(seconds=5))
    rid = post(client, exclude=[store.sim.ids(4)[10]]).json()["requestId"]
    assert store.lots[rid]["lot"].profile.echo_topic is None
    assert store.request(rid)["profile"]["echoTopic"] is None


def test_empty_catalog_returns_empty_items_and_saves_nothing(empty_client, store):
    assert post(empty_client).json()["items"] == [] and store.lots == {}


def test_catalog_without_rows_returns_empty_items_and_saves_nothing(client, store, cache):
    store.drop_articles(store.sim.catalog.ids)
    assert cache.refresh() and cache.get().ids == []
    assert post(client).json()["items"] == [] and store.lots == {}


def test_no_topics_at_all_returns_empty_items(client, store):    # Review Focus 3
    store.topic_list = []
    assert post(client).json()["items"] == []


def test_healthz(client, empty_client):
    assert client.get("/healthz").json() == {
        "status": "ok", "catalogSize": 1800, "catalogAgeSeconds": 0.0,
    }
    assert empty_client.get("/healthz").json() == {
        "status": "ok", "catalogSize": 0, "catalogAgeSeconds": None,
    }


def test_redis_seen_reads_the_users_set():
    r = fakeredis.FakeRedis(decode_responses=True)
    r.sadd("seen:u_1", "a_1", "a_2")
    r.sadd("seen:u_2", "a_3")
    assert RedisSeen(r).seen("u_1") == frozenset({"a_1", "a_2"})
    assert RedisSeen(r).seen("u_nope") == frozenset()
