from datetime import UTC, datetime
from functools import reduce

import pytest

from swipread_reco.models import Event
from swipread_reco.params import Params
from swipread_reco.profile import ArticleState, apply_event, article_value

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def ev(type, payload=None, word_count=230):
    return Event(
        event_id="e1",
        type=type,
        article_id="a1",
        occurred_at=T0,
        received_at=T0,
        payload=payload or {},
        topic_id=1,
        word_count=word_count,
    )


def value(*events):
    params = Params()
    state = reduce(lambda s, e: apply_event(s, e, params), events, ArticleState())
    return article_value(state, params)


@pytest.mark.parametrize("events, expected", [
    ([ev("view", {"dwellMs": 1000})], -0.02),
    ([ev("view", {"dwellMs": 1500})], 0.0),          # borne : pas zappé à 1500
    ([ev("view", {"dwellMs": 3000})], 0.0),
    ([ev("view", {"dwellMs": 6000})], 0.02),         # borne : vue longue à 6000
    ([ev("open")], 0.03),
    ([ev("read", {"readMs": 30000, "scrollDepth": 1.0})], 0.05),   # c = 0,5
    ([ev("read", {"readMs": 600000, "scrollDepth": 0.1})], 0.07),  # c borné à 1
    ([ev("read", {"readMs": 0, "scrollDepth": 0})], 0.03),
    ([ev("like")], 0.08), ([ev("save")], 0.08), ([ev("share", {"channel": "x"})], 0.08),
    ([ev("hide")], -0.15),
    ([ev("view", {"dwellMs": 500}), ev("like")], 0.08),            # le positif l'emporte
    ([ev("like"), ev("unlike")], 0.0),                              # A8
    ([ev("open"), ev("like"), ev("unlike")], 0.03),
    ([ev("like"), ev("hide")], -0.15),
    ([ev("hide"), ev("like")], -0.15),                              # masquage définitif
    ([ev("impression")], 0.0),
])
def test_value(events, expected):
    assert value(*events) == pytest.approx(expected)


def test_read_without_word_count_uses_60s():
    assert value(ev("read", {"readMs": 60000, "scrollDepth": 0}, word_count=None)) == pytest.approx(0.07)
    assert value(ev("read", {"readMs": 30000, "scrollDepth": 0}, word_count=0)) == pytest.approx(0.05)


@pytest.mark.parametrize("bad", [
    ev("view", {}), ev("view", {"dwellMs": "abc"}), ev("view", {"dwellMs": None}),
    ev("read", {"scrollDepth": 0.9}), ev("read", {"readMs": -5}),
])
def test_malformed_payload_is_ignored(bad):      # Review Focus 1
    assert apply_event(ArticleState(), bad, Params()) == ArticleState()
