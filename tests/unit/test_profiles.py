"""Stored profiles (reco.user_profiles, contract v1.5): JSON of the state, freshness rules."""

import dataclasses
from datetime import timedelta

from swipread_reco.params import Params
from swipread_reco.profile import replay_profile
from swipread_reco.profiles import current_profile, profile_basis
from swipread_reco.store import state_from_json, state_json

from ..fakes import NOW, FakeStore

TOPIC_IDS = list(range(1, 11))
CHOSEN = frozenset({1, 2, 3})


def store_with_events() -> FakeStore:
    store = FakeStore()
    ids = store.sim.catalog.ids
    for k in range(30):
        kind = ("like", "hide", "open")[k % 3]
        store.add_event("u_1", kind, ids[k], occurred_at=NOW - timedelta(hours=40 - k))
    store.add_event("u_1", "view", ids[40], occurred_at=NOW - timedelta(hours=1),
                    payload={"dwellMs": 300})
    return store


def test_state_json_roundtrip_is_exact():
    store = store_with_events()
    state = replay_profile(store.events("u_1", 5000), [7, 3, 1, 2], CHOSEN, Params())
    back = state_from_json(state_json(state))
    assert back == state
    assert list(back.weights) == [7, 3, 1, 2]


def test_first_request_replays_and_stores_then_reuses():
    store = store_with_events()
    first = current_profile(store, "u_1", TOPIC_IDS, CHOSEN, Params(), NOW)
    assert store.profile_saves == 1
    again = current_profile(store, "u_1", TOPIC_IDS, CHOSEN, Params(), NOW)
    assert again == first and store.profile_saves == 1


def test_a_new_event_not_yet_consumed_makes_the_profile_stale():
    store = store_with_events()
    current_profile(store, "u_1", TOPIC_IDS, CHOSEN, Params(), NOW)
    article = store.sim.catalog.ids[100]
    store.add_event("u_1", "hide", article, occurred_at=NOW)
    state = current_profile(store, "u_1", TOPIC_IDS, CHOSEN, Params(), NOW)
    assert article in state.touched_article_ids and store.profile_saves == 2


def test_an_event_committed_late_with_an_older_date_is_caught_by_the_count():
    store = store_with_events()
    current_profile(store, "u_1", TOPIC_IDS, CHOSEN, Params(), NOW)
    article = store.sim.catalog.ids[101]
    store.add_event("u_1", "hide", article, occurred_at=NOW - timedelta(days=3))
    assert article in current_profile(store, "u_1", TOPIC_IDS, CHOSEN, Params(), NOW).touched_article_ids


def test_changed_chosen_topics_topics_or_parameters_make_it_stale():
    store = store_with_events()
    current_profile(store, "u_1", TOPIC_IDS, CHOSEN, Params(), NOW)
    current_profile(store, "u_1", TOPIC_IDS, frozenset({4}), Params(), NOW)
    assert store.profile_saves == 2
    current_profile(store, "u_1", TOPIC_IDS + [11], frozenset({4}), Params(), NOW)
    assert store.profile_saves == 3
    current_profile(store, "u_1", TOPIC_IDS + [11], frozenset({4}), Params(epsilon=0.2), NOW)
    assert store.profile_saves == 4


def test_basis_names_what_a_replay_depends_on():
    basis = profile_basis([2, 1], {3, 1}, Params())
    assert basis["topics"] == [2, 1] and basis["chosen"] == [1, 3]
    assert basis["params"] == Params().as_dict() and basis["modelVersion"] == "topic-weights-v1"
    assert profile_basis([2, 1], {1, 3}, dataclasses.replace(Params(), w_floor=0.02)) != basis
