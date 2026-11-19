"""E3 to E7: a reader's feed through the backend, served by the reco (brief, scenarios)."""

from datetime import UTC, datetime

import httpx
import numpy as np
import pytest
import redis

from swipread_reco.models import Event
from swipread_reco.params import MODEL_VERSION, Params
from swipread_reco.profile import build_profile, initial_weights

from .client import (
    RECO_REASONS,
    Backend,
    backend_reco_failures,
    event,
    last_request_at,
    lot_articles,
    requests_after,
    settle,
    topic_of,
    unserved_articles,
    wait_for,
)
from .seed import DUP_ID, DUP_OF

pytestmark = pytest.mark.e2e

CHOSEN = ("tech", "economy", "culture")
X, Y = "sport", "politics"   # topics the readers did not choose
EXPLANATION_KEYS = {
    "affinity", "freshness", "penalty", "topicWeight", "nearestLiked", "nearestRejected",
    "relaxedSource",
}


def _weights(reco: httpx.Client, user_id: str) -> dict[str, float]:
    r = reco.get(f"/v1/explain/users/{user_id}")
    assert r.status_code == 200, r.text
    return {t["slug"]: t["weight"] for t in r.json()["topics"]}


def _new_reader(backend: Backend, rds: redis.Redis, topic_ids: dict[str, int],
                chosen: tuple[str, ...] = CHOSEN):
    """Registered reader with `chosen` topics, first page read and stock refilled by the reco."""
    reader = backend.register()
    reader.choose_topics(topic_ids[s] for s in chosen)
    first = reader.feed()
    settle(rds, reader.user_id)
    return reader, first


def test_e3_new_reader(backend, rds, admin, topic_ids, measure):
    reader = backend.register()
    chosen = {topic_ids[s] for s in CHOSEN}
    reader.choose_topics(chosen)

    pages = []
    for n in range(5):
        if n:
            settle(rds, reader.user_id)
        pages.append(reader.feed())
        assert pages[-1], f"page {n + 1} is empty"

    served = [c["articleId"] for page in pages for c in page]
    assert len(served) == len(set(served)), "an article was served twice"
    later = [c for page in pages[1:] for c in page]
    assert all(c["requestId"].startswith("r_") for c in later), (
        [c["requestId"] for c in later], backend_reco_failures(reader.user_id))
    assert all(c["reason"] in RECO_REASONS for c in later), [c["reason"] for c in later]

    on_chosen = sum(topic_of(c) in chosen for c in later) / len(later)
    request_ids = sorted({c["requestId"] for c in later})
    rows = admin.execute(
        "SELECT request_id, model_version FROM reco.feed_requests WHERE request_id = ANY(%s)",
        (request_ids,),
    ).fetchall()
    measure("E3", first_page_request_ids=sorted({c["requestId"] for c in pages[0]}),
            reco_cards=len(later), share_on_chosen=round(on_chosen, 3),
            reasons=sorted({c["reason"] for c in later}), request_ids=request_ids)
    assert on_chosen >= 0.7
    assert dict(rows) == {r: MODEL_VERSION for r in request_ids}


def test_e4_likes_on_unchosen_topic(backend, reco, rds, admin, topic_ids, measure):
    reader, _ = _new_reader(backend, rds, topic_ids)
    x = topic_ids[X]
    all_topics = sorted(topic_ids.values())
    chosen = {topic_ids[s] for s in CHOSEN}
    before = last_request_at(admin, reader.user_id)

    liked = unserved_articles(admin, rds, reader.user_id, x, 2)
    likes = [event("like", a) for a in liked]
    reader.send(*likes)

    now = datetime.now(UTC)
    expected = build_profile(
        [Event(e["eventId"], "like", e["articleId"], now, now, {}, x, None) for e in likes],
        all_topics, chosen, now, None, Params(),
    ).weights[x]
    initial = initial_weights(all_topics, chosen, Params())[x]
    weight = _weights(reco, reader.user_id)[X]
    after = wait_for(lambda: requests_after(admin, reader.user_id, before),
                     "a reco lot computed after the likes")

    pages = []
    for _ in range(4):
        settle(rds, reader.user_id)
        pages.append(reader.feed())
    x_cards = [c["articleId"] for page in pages for c in page if topic_of(c) == x]
    measure("E4", initial_weight=round(initial, 4), weight=round(weight, 4),
            expected=round(expected, 4), lots_after_likes=len(after), x_cards_in_4_pages=x_cards)
    assert weight > initial
    assert weight == pytest.approx(expected, abs=1e-3)
    assert x_cards, ("no card of the liked topic in the next 4 pages",
                     backend_reco_failures(reader.user_id))


def test_e5_hide_on_unchosen_topic(backend, reco, rds, admin, topic_ids, measure):
    reader, _ = _new_reader(backend, rds, topic_ids)
    y = topic_ids[Y]
    before = last_request_at(admin, reader.user_id)

    reader.send(event("hide", unserved_articles(admin, rds, reader.user_id, y, 1)[0]))
    weight = _weights(reco, reader.user_id)[Y]
    wait_for(lambda: requests_after(admin, reader.user_id, before),
             "a reco lot computed after the hide")
    for _ in range(4):
        settle(rds, reader.user_id)
        reader.feed()
    settle(rds, reader.user_id)

    after = requests_after(admin, reader.user_id, before)
    items = lot_articles(admin, after)
    measure("E5", weight=weight, lots_after_hide=len(after), items_after_hide=len(items),
            items_of_y=sum(t == y for _, t in items))
    assert weight == 0
    assert items
    assert not [a for a, t in items if t == y]


def test_e6_explanation_matches_served_cards(backend, reco, rds, topic_ids, measure):
    reader, _ = _new_reader(backend, rds, topic_ids)
    cards = reader.feed() + reader.feed()
    request_id = cards[0]["requestId"]
    assert request_id.startswith("r_"), backend_reco_failures(reader.user_id)
    served = [c for c in cards if c["requestId"] == request_id]

    r = reco.get(f"/v1/explain/requests/{request_id}")
    assert r.status_code == 200, r.text
    items = sorted(r.json()["items"], key=lambda i: i["position"])
    ids = [i["articleId"] for i in items]
    positions = [ids.index(c["articleId"]) for c in served]   # ValueError if absent
    measure("E6", request_id=request_id, served_from_request=len(served), lot_size=len(items),
            positions_in_lot=positions)
    assert positions == sorted(positions), "served cards are not in the lot's order"
    by_id = {i["articleId"]: i for i in items}
    assert all(by_id[c["articleId"]]["reason"] == c["reason"] for c in served)
    for item in items:
        assert 0 <= item["score"] <= 1
        assert set(item["explanation"]) == EXPLANATION_KEYS, item["explanation"]


def test_e7_no_same_story_after_like(backend, reco, rds, admin, topic_ids, measure):
    topic = admin.execute(
        "SELECT topic_id FROM app.article_topics WHERE article_id = %s", (DUP_OF,)
    ).fetchone()[0]
    vectors = dict(admin.execute(
        "SELECT article_id, embedding FROM reco.article_embeddings WHERE article_id = ANY(%s)",
        ([DUP_OF, DUP_ID],),
    ).fetchall())
    cos = float(np.dot(vectors[DUP_OF].to_numpy(), vectors[DUP_ID].to_numpy()))
    others = [s for s in CHOSEN if topic_ids[s] != topic][:2]
    reader = backend.register()
    reader.choose_topics([topic] + [topic_ids[s] for s in others])

    # Control: before the like, the near-duplicate (freshest article of its topic) is recommended.
    r = reco.post("/v1/recommendations", json={"userId": reader.user_id, "count": 50})
    assert r.status_code == 200, r.text
    assert DUP_ID in [i["articleId"] for i in r.json()["items"]]
    before = last_request_at(admin, reader.user_id)

    reader.send(event("like", DUP_OF))
    wait_for(lambda: requests_after(admin, reader.user_id, before), "a reco lot after the like")
    served = []
    for _ in range(4):
        settle(rds, reader.user_id)
        served += [c["articleId"] for c in reader.feed()]
    settle(rds, reader.user_id)

    after = requests_after(admin, reader.user_id, before)
    lots = [aid for aid, _ in lot_articles(admin, after)]
    measure("E7", cos_dup=round(cos, 4), lots_after_like=len(after), items=len(lots))
    assert cos > 0.95
    assert DUP_ID not in lots
    assert DUP_ID not in served
