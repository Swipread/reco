from collections import Counter

import pytest

from swipread_reco.allocation import (
    TopicSequence,
    cap_shares,
    seed_from_request_id,
    topic_offsets,
)


def test_cap_redistributes_pro_rata():
    assert cap_shares({1: 0.79, 2: 0.14, 3: 0.07}, 0.60) == pytest.approx(
        {1: 0.60, 2: 0.26667, 3: 0.13333}, abs=1e-4
    )


def test_cap_iterates_until_none_exceeds():
    assert cap_shares({1: 0.50, 2: 0.45, 3: 0.05}, 0.40) == pytest.approx(
        {1: 0.40, 2: 0.40, 3: 0.20}
    )


def test_cap_not_applied_when_others_are_zero_and_noop_below_cap():
    assert cap_shares({1: 1.0, 2: 0.0}, 0.60) == {1: 1.0, 2: 0.0}
    assert cap_shares({1: 0.5, 2: 0.5}, 0.60) == {1: 0.5, 2: 0.5}


def test_seed_is_stable_and_fits_bigint():
    s = seed_from_request_id("r_abc")
    assert s == seed_from_request_id("r_abc") != seed_from_request_id("r_abd")
    assert 0 <= s < 2**63


def test_offsets():
    o = topic_offsets({1: 0.5, 2: 0.5, 3: 0.0}, echo_topic=2, seed=7)
    assert set(o) == {1, 2} and o[2] == 0.0 and 0.0 <= o[1] < 1.0
    assert o[1] == topic_offsets({1: 0.5, 2: 0.5, 3: 0.0}, echo_topic=None, seed=7)[1]


def drain(seq, n):
    out = []
    while len(out) < n and (t := seq.pop()) is not None:
        out.append(t)
        seq.advance(t)
    return out


def test_sequence_interleaves():
    assert drain(TopicSequence({1: 0.5, 2: 0.5}, {1: 0.0, 2: 0.5}), 6) == [1, 2, 1, 2, 1, 2]


def test_sequence_is_proportional_on_50_slots():
    seq = TopicSequence({1: 0.3, 2: 0.3, 3: 0.3, 4: 0.1}, {t: 0.5 for t in (1, 2, 3, 4)})
    assert Counter(drain(seq, 50)) == {1: 15, 2: 15, 3: 15, 4: 5}


def test_ties_go_to_smallest_topic_id():
    assert drain(TopicSequence({2: 0.5, 1: 0.5}, {1: 0.0, 2: 0.0}), 2) == [1, 2]


def test_exhausted_topic_gives_its_slots_to_others():
    seq = TopicSequence({1: 0.5, 2: 0.5}, {1: 0.0, 2: 0.5})
    assert seq.pop() == 1
    seq.advance(1)
    assert seq.pop() == 2                       # jamais avancé : épuisé
    assert drain(seq, 3) == [1, 1, 1]
    seq2 = TopicSequence({1: 1.0}, {1: 0.0})
    seq2.pop()
    assert seq2.pop() is None


def test_sequence_start_counts_cards_already_queued():
    shares, offsets = {1: 0.5, 2: 0.5}, {1: 0.0, 2: 0.0}
    assert drain(TopicSequence(shares, offsets, start={1: 3}), 8) == [2, 2, 2, 1, 2, 1, 2, 1]
    assert drain(TopicSequence(shares, offsets, start={}), 8) == drain(
        TopicSequence(shares, offsets), 8)
    seq = TopicSequence({1: 0.0, 2: 1.0}, {2: 0.3}, start={1: 2})   # zero share stays ignored
    assert drain(seq, 3) == [2, 2, 2]
