import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from swipread_reco.models import Event
from swipread_reco.params import Params
from swipread_reco.profile import (
    PROFILE_HISTORY_MAX,
    build_profile,
    echo_candidate,
    finish_profile,
    initial_weights,
    replay_profile,
)

TOPICS = list(range(1, 11))
CHOSEN = {1, 2, 3}
NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)


def ev(type, article_id, topic_id, minutes_ago, payload=None, received_minutes_ago=None):
    occurred = NOW - timedelta(minutes=minutes_ago)
    received = occurred if received_minutes_ago is None else NOW - timedelta(minutes=received_minutes_ago)
    return Event(
        event_id=f"{type}-{article_id}-{minutes_ago}",
        type=type,
        article_id=article_id,
        occurred_at=occurred,
        received_at=received,
        payload=payload or {},
        topic_id=topic_id,
        word_count=None,
    )


def test_initial_weights():
    w = initial_weights(TOPICS, CHOSEN, Params())
    assert w[1] == pytest.approx(0.30) and w[4] == pytest.approx(0.1 / 7)
    assert sum(w.values()) == pytest.approx(1.0)
    assert initial_weights(TOPICS, set(), Params())[5] == pytest.approx(0.1)
    assert initial_weights(TOPICS, set(TOPICS), Params())[5] == pytest.approx(0.1)


def test_empty_topic_list_gives_empty_weights_and_no_echo():
    assert initial_weights([], CHOSEN, Params()) == {}
    p = build_profile([ev("like", "a", 4, 5)], [], CHOSEN, NOW, None, Params())
    assert p.weights == {}
    assert p.echo_topic is None
    assert p.history == []
    assert [i.article_id for i in p.liked] == ["a"]


def test_worked_example_of_spec_4_2():
    events = [ev("like", "s1", 4, 30), ev("like", "s2", 4, 20), ev("hide", "p1", 5, 10)]
    w = build_profile(events, TOPICS, CHOSEN, NOW, None, Params()).weights
    assert w[1] == pytest.approx(0.26039, abs=1e-4)
    assert w[4] == pytest.approx(0.15683, abs=1e-4)
    assert w[5] == 0.0
    assert w[6] == pytest.approx(0.01240, abs=1e-4)


def test_five_likes_on_unchosen_topic_reach_30_percent():      # A3
    events = [ev("like", f"s{i}", 4, 50 - i) for i in range(5)]
    assert build_profile(events, TOPICS, CHOSEN, NOW, None, Params()).weights[4] >= 0.30


def test_one_article_counts_once():                             # A7
    events = [ev("view", "a", 1, 9, {"dwellMs": 7000}), ev("open", "a", 1, 8),
              ev("read", "a", 1, 7, {"readMs": 10**7, "scrollDepth": 1}), ev("like", "a", 1, 6)]
    p = build_profile(events, TOPICS, CHOSEN, NOW, None, Params())
    assert sum(h.delta for h in p.history) == pytest.approx(0.08)


def test_chosen_topic_never_below_floor():
    events = [ev("hide", f"a{i}", 1, 50 - i) for i in range(6)]
    assert build_profile(events, TOPICS, CHOSEN, NOW, None, Params()).weights[1] >= 0.01


def test_skip_does_not_extinguish_unchosen_topic():
    w = build_profile([ev("view", "a", 4, 5, {"dwellMs": 300})], TOPICS, CHOSEN, NOW, None, Params()).weights
    assert w[4] > 0.0


def test_liked_unchosen_topic_survives_first_hide_then_dies():
    likes = [ev("like", f"s{i}", 4, 60 - i) for i in range(3)]          # ≈ 21,8 %
    one = build_profile(likes + [ev("hide", "h1", 4, 5)], TOPICS, CHOSEN, NOW, None, Params()).weights[4]
    two = build_profile(likes + [ev("hide", "h1", 4, 5), ev("hide", "h2", 4, 4)], TOPICS, CHOSEN, NOW, None, Params()).weights[4]
    assert one > 0.0 and two == 0.0


def test_order_and_duplicates():
    a, b = ev("like", "x", 1, 10), ev("unlike", "x", 1, 5)
    p1 = build_profile([a, b], TOPICS, CHOSEN, NOW, None, Params())
    p2 = build_profile([b, a, a], TOPICS, CHOSEN, NOW, None, Params())
    assert p1.weights == p2.weights


def test_liked_and_rejected_lists():
    old = NOW - timedelta(days=30)
    events = [ev("like", "l", 1, 0), ev("open", "o", 1, 0), ev("view", "s", 2, 0, {"dwellMs": 100}),
              ev("hide", "h", 3, 0), dataclasses.replace(ev("like", "old", 1, 0), occurred_at=old)]
    p = build_profile(events, TOPICS, CHOSEN, NOW, None, Params())
    liked = {i.article_id: i.strength for i in p.liked}
    rejected = {i.article_id: i.strength for i in p.rejected}
    assert liked == pytest.approx({"l": 1.0, "o": 0.375, "old": 0.5})
    assert rejected == pytest.approx({"s": 0.15, "h": 1.0})
    assert p.touched_article_ids == {"l", "o", "s", "h", "old"}


def test_lists_keep_most_recent_items():
    events = [ev("like", f"a{i}", 1, i) for i in range(150)]
    p = build_profile(events, TOPICS, CHOSEN, NOW, None, Params())
    assert len(p.liked) == 100 and p.liked[0].article_id == "a0"


def test_unknown_topic_changes_no_weight_but_is_listed():
    p = build_profile([ev("like", "x", None, 5)], TOPICS, CHOSEN, NOW, None, Params())
    assert p.weights == initial_weights(TOPICS, CHOSEN, Params())
    assert [i.article_id for i in p.liked] == ["x"]


def test_echo_topic():
    last = NOW - timedelta(minutes=10)
    mk = lambda e: build_profile(e, TOPICS, CHOSEN, NOW, last, Params()).echo_topic
    assert mk([ev("like", "a", 4, 5, received_minutes_ago=5)]) == 4
    assert mk([ev("like", "a", 4, 20, received_minutes_ago=20)]) is None       # déjà pris en compte
    assert mk([ev("read", "a", 2, 5, {"readMs": 10, "scrollDepth": 0.9}, 5)]) == 2
    assert mk([ev("read", "a", 2, 5, {"readMs": 10**6, "scrollDepth": 0.8}, 5)]) is None  # strictement > 0,8
    assert mk([ev("hide", "a", 4, 5, received_minutes_ago=5)]) is None
    assert mk([ev("like", "a", 4, 6, received_minutes_ago=6), ev("save", "b", 2, 5, received_minutes_ago=5)]) == 2
    assert mk([ev("hide", "h", 5, 6, received_minutes_ago=6), ev("like", "a", 5, 5, received_minutes_ago=5)]) == 5


def test_echo_ignored_when_topic_weight_is_zero():
    events = [ev("like", "a", 5, 9, received_minutes_ago=9), ev("hide", "a", 5, 8, received_minutes_ago=8)]
    assert build_profile(events, TOPICS, CHOSEN, NOW, NOW - timedelta(minutes=10), Params()).echo_topic is None


def test_unsave_never_triggers_the_echo():
    last = NOW - timedelta(minutes=10)
    mk = lambda e: build_profile(e, TOPICS, CHOSEN, NOW, last, Params()).echo_topic
    assert mk([ev("unsave", "a", 4, 5, received_minutes_ago=5)]) is None
    assert mk([ev("save", "a", 4, 20, received_minutes_ago=20),
               ev("unsave", "a", 4, 5, received_minutes_ago=5)]) is None


def test_unsave_takes_back_the_value_of_save():
    events = [ev("open", "a", 4, 15), ev("save", "a", 4, 10), ev("unsave", "a", 4, 5)]
    p = build_profile(events, TOPICS, CHOSEN, NOW, None, Params())
    assert [h.type for h in p.history] == ["open", "save", "unsave"]
    assert [h.delta for h in p.history] == pytest.approx([0.03, 0.05, -0.05])
    assert {i.article_id: i.strength for i in p.liked} == pytest.approx({"a": 0.375}, rel=1e-3)


def read(payload, word_count):
    return dataclasses.replace(ev("read", "a", 2, 5, payload, 5), word_count=word_count)


# The backend's strong read (StrongEventPolicy.java): scrollDepth > 0.8, or readMs (truncated)
# >= 0.6 x word_count / 230 wpm, never with an unknown or zero word count.
# 230 words: 60 000 ms expected, threshold 36 000 ms. 1 000 words: threshold 156 521.739… ms.
@pytest.mark.parametrize("payload, word_count, strong", [
    ({"readMs": 10, "scrollDepth": 0.81}, 230, True),        # lecture profonde
    ({"readMs": 10, "scrollDepth": 0.9}, None, True),        # profonde, longueur inconnue
    ({"readMs": 10**6, "scrollDepth": 0.8}, None, False),    # strictement > 0,8
    ({"readMs": 0, "scrollDepth": 0.8}, 230, False),
    ({"readMs": 36000}, 230, True),                          # pas de scrollDepth : borne atteinte
    ({"readMs": 35999}, 230, False),
    ({"readMs": 35999.9}, 230, False),                       # readMs tronqué (longValue)
    ({"readMs": 36000.5}, 230, True),
    ({"readMs": 36000, "scrollDepth": 0.1}, 230, True),      # longue mais peu défilée
    ({"readMs": 156521}, 1000, False),
    ({"readMs": 156522}, 1000, True),
    ({"readMs": 10**7}, None, False),                        # longueur inconnue
    ({"readMs": 10**7}, 0, False),                           # longueur nulle
    ({"readMs": 10**7}, -5, False),
    ({}, 230, False),                                        # readMs absent compte pour 0
    ({"readMs": "long"}, 230, False),
])
def test_echo_read_rule_mirrors_the_backend(payload, word_count, strong):
    last = NOW - timedelta(minutes=10)
    p = build_profile([read(payload, word_count)], TOPICS, CHOSEN, NOW, last, Params())
    assert (p.echo_topic == 2) is strong


def _mixed_events():
    events = [ev("like", f"l{k}", 1 + k % 4, 600 - k) for k in range(80)]
    events += [ev("hide", f"h{k}", 5, 300 - k) for k in range(5)]
    events += [ev("view", "s", 6, 20, {"dwellMs": 300}), ev("open", "o", 2, 10)]
    events += [ev("save", "l3", 4, 5, received_minutes_ago=1)]
    return events


def test_replay_then_finish_is_build_profile():
    events, last = _mixed_events(), NOW - timedelta(minutes=3)
    state = replay_profile(events, TOPICS, CHOSEN, Params())
    later = NOW + timedelta(days=12)
    assert finish_profile(state, echo_candidate(events, last), later, Params()) == build_profile(
        events, TOPICS, CHOSEN, later, last, Params()
    )


def test_replay_does_not_depend_on_the_clock_nor_the_previous_request():
    state = replay_profile(_mixed_events(), TOPICS, CHOSEN, Params())
    assert state.liked[0].strength == 1.0     # before the age factor
    old = finish_profile(state, None, NOW + timedelta(days=30), Params())
    assert old.liked[0].strength == pytest.approx(0.5, rel=1e-3)


def test_echo_candidate_reads_only_events_received_after_the_previous_request():
    events = _mixed_events()
    assert echo_candidate(events, NOW - timedelta(minutes=3)) == 4
    assert echo_candidate(events, NOW) is None
    assert echo_candidate(events[-1:], NOW - timedelta(minutes=3)) == 4   # just those suffice


def test_finish_profile_drops_an_echo_topic_without_weight():
    state = replay_profile([], TOPICS, CHOSEN, Params())
    assert finish_profile(state, 99, NOW, Params()).echo_topic is None


def test_state_keeps_the_last_weight_changes_only():
    events = [ev("like", f"a{k}", 1 + k % 3, 1000 - k) for k in range(PROFILE_HISTORY_MAX + 20)]
    full = replay_profile(events, TOPICS, CHOSEN, Params())
    assert len(full.history) == PROFILE_HISTORY_MAX
    assert full.history[-1].article_id == f"a{PROFILE_HISTORY_MAX + 19}"
