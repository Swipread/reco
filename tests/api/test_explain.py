"""GET /v1/explain/users/{userId} and GET /v1/explain/requests/{requestId} (spec 9.2)."""

from datetime import timedelta

import pytest

from swipread_reco.params import SCORE_DECIMALS, Params

from ..fakes import NOW
from .test_recommendations import post


def test_user_profile_matches_spec_example(client, store):
    for i, (t, a) in enumerate([("like", store.sim.ids(4)[0]), ("like", store.sim.ids(4)[1]), ("hide", store.sim.ids(5)[0])]):
        store.add_event("u_1", t, a, occurred_at=NOW - timedelta(minutes=30 - 10 * i))
    b = client.get("/v1/explain/users/u_1").json(); by = {t["id"]: t for t in b["topics"]}
    assert by[4]["weight"] == pytest.approx(0.15683, abs=1e-3) and by[5]["weight"] == 0.0 and by[1]["chosen"] is True
    assert sum(t["share"] for t in b["topics"]) == pytest.approx(1.0)
    assert [h["type"] for h in b["history"]] == ["hide", "like", "like"] and b["history"][0]["delta"] < 0
    assert len(b["liked"]) == 2 and b["liked"][0]["title"] and len(b["rejected"]) == 1


def test_user_response_shape_and_order(client, store):
    a = store.sim.ids(4)[0]
    store.add_event("u_1", "like", a, occurred_at=NOW - timedelta(hours=1))
    b = client.get("/v1/explain/users/u_1").json()
    assert set(b) == {"userId", "topics", "liked", "rejected", "history"} and b["userId"] == "u_1"
    assert set(b["topics"][0]) == {"id", "slug", "label", "chosen", "weight", "share"}
    keys = [(-t["weight"], t["id"]) for t in b["topics"]]
    assert keys == sorted(keys) and len(keys) == 10
    assert {t["id"] for t in b["topics"] if t["chosen"]} == {1, 2, 3}
    assert b["topics"][0]["slug"] == "t1" and b["topics"][0]["label"] == "Topic 1"
    assert b["liked"] == [{"articleId": a, "title": f"Title {a}", "strength": pytest.approx(1.0, abs=1e-3)}]
    weight_4 = next(t["weight"] for t in b["topics"] if t["id"] == 4)
    assert b["history"] == [{
        "eventId": "e_0", "type": "like", "articleId": a, "topicSlug": "t4",
        "delta": pytest.approx(Params().v_like), "weightAfter": weight_4,
        "occurredAt": "2026-11-10T11:00:00Z",
    }]


def test_history_is_capped_at_50(client, store):
    ids = store.sim.ids(4)[:60]
    for k, a in enumerate(ids):                                  # 60 likes, each moves topic 4
        store.add_event("u_1", "like", a, occurred_at=NOW - timedelta(minutes=60 - k))
    history = client.get("/v1/explain/users/u_1").json()["history"]
    assert len(history) == 50
    assert [h["articleId"] for h in history] == list(reversed(ids))[:50]
    times = [h["occurredAt"] for h in history]
    assert times == sorted(times, reverse=True) and times[0] == "2026-11-10T11:59:00Z"


def test_user_profile_reads_the_same_events_as_recommendations(client, store):
    client.get("/v1/explain/users/u_1")
    assert store.events_limit == Params().events_max


def test_user_weights_and_shares_equal_those_of_the_lot(client, store):
    for k, a in enumerate(store.sim.ids(6)[:5]):
        store.add_event("u_1", "like", a, occurred_at=NOW - timedelta(minutes=10 - k))
    store.add_event("u_1", "hide", store.sim.ids(2)[0], occurred_at=NOW - timedelta(minutes=1))
    profile = client.get(f"/v1/explain/requests/{post(client).json()['requestId']}").json()["profile"]
    topics = client.get("/v1/explain/users/u_1").json()["topics"]
    assert {t["slug"]: t["weight"] for t in topics} == profile["weights"]
    assert {t["slug"]: t["share"] for t in topics} == profile["shares"]


def test_liked_and_rejected_keep_only_articles_the_algorithm_can_use(client, store):
    kept, gone = store.sim.ids(4)[:2]
    store.add_event("u_1", "like", kept, occurred_at=NOW - timedelta(minutes=2))
    store.add_event("u_1", "like", gone, occurred_at=NOW - timedelta(minutes=1))
    store.delete_articles([gone])                                # no embedding: left out by recommend
    b = client.get("/v1/explain/users/u_1").json()
    assert [i["articleId"] for i in b["liked"]] == [kept]
    assert len(b["history"]) == 2                                # its weight change still counts


def test_request_explanation(client):
    rid = post(client, count=5).json()["requestId"]; b = client.get(f"/v1/explain/requests/{rid}").json()
    assert [i["position"] for i in b["items"]] == [0, 1, 2, 3, 4] and b["requestedCount"] == 5
    assert {"affinity", "freshness", "penalty", "topicWeight"} <= set(b["items"][0]["explanation"])
    assert b["items"][0]["title"] and b["items"][0]["topicSlug"]


def test_request_explanation_is_the_saved_lot(client, store):
    rid = post(client, count=5, context={"sessionId": "s_7"}).json()["requestId"]
    b = client.get(f"/v1/explain/requests/{rid}").json()
    saved = store.lots[rid]
    lot = saved["lot"]
    assert set(b) == {
        "requestId", "userId", "sessionId", "modelVersion", "requestedCount", "seed",
        "generatedAt", "profile", "params", "items",
    }
    assert (b["requestId"], b["userId"], b["sessionId"]) == (rid, "u_1", "s_7")
    assert (b["modelVersion"], b["seed"], b["generatedAt"]) == (
        "topic-weights-v1", lot.seed, "2026-11-10T12:00:00Z"
    )
    assert b["params"] == Params().as_dict()
    assert set(b["profile"]) == {"weights", "shares", "likedCount", "rejectedCount", "echoTopic", "mu"}
    assert set(b["items"][0]) == {
        "position", "articleId", "title", "topicSlug", "reason", "score", "explanation",
    }
    assert [(i["articleId"], i["topicSlug"], i["reason"], i["score"], i["explanation"])
            for i in b["items"]] == [
        (i.article_id, f"t{i.topic_id}", i.reason, round(i.score, SCORE_DECIMALS), i.explanation)
        for i in lot.items
    ]


def test_request_scores_equal_the_recommendations_response_scores(client):
    response = post(client).json()
    items = client.get(f"/v1/explain/requests/{response['requestId']}").json()["items"]
    assert [i["score"] for i in items] == [i["score"] for i in response["items"]]


def test_request_item_of_a_deleted_article_has_null_title(client, store):
    rid = post(client, count=3).json()["requestId"]
    first = store.lots[rid]["lot"].items[0].article_id
    store.delete_articles([first])
    items = client.get(f"/v1/explain/requests/{rid}").json()["items"]
    assert items[0]["title"] is None and items[1]["title"]


def test_unknown_user_and_request_are_404(client):
    assert client.get("/v1/explain/users/u_nope").status_code == 404
    assert client.get("/v1/explain/requests/r_nope").status_code == 404


def test_store_failure_is_503(client, store):
    rid = post(client, count=3).json()["requestId"]
    store.fail = True
    assert client.get("/v1/explain/users/u_1").status_code == 503
    assert client.get(f"/v1/explain/requests/{rid}").status_code == 503
