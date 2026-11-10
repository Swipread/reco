"""Acceptance tests of spec 11.2 (A1 to A12) on the synthetic catalog."""

from collections import Counter

import numpy as np
import pytest

from swipread_reco.models import Event
from swipread_reco.scoring import sim
from swipread_reco.sim.catalog import make_sim_catalog

from .helpers import CHOSEN, NOW, X, Y, ev, run, subs, topics

S = make_sim_catalog(NOW)


def test_a01_new_reader():
    c = Counter(topics(run(S)))
    assert 40 <= sum(c[t] for t in CHOSEN) <= 48 and all(13 <= c[t] <= 17 for t in CHOSEN)


def test_a02_two_likes_then_hide():
    e = [ev("like", S.ids(X)[0], S, 30), ev("like", S.ids(X)[1], S, 20),
         ev("hide", S.ids(Y)[0], S, 10)]
    lot = run(S, e)
    t = topics(lot)
    w = lot.profile.weights
    assert w[1] == pytest.approx(0.26039, abs=1e-3) and w[X] == pytest.approx(0.15683, abs=1e-3)
    assert w[Y] == 0.0
    assert t[0] == X and Y not in t and sum(x in CHOSEN for x in t[:10]) >= 6


def test_a03_five_likes():
    e = [ev("like", S.ids(X)[i], S, 50 - i) for i in range(5)]
    assert run(S, e).profile.weights[X] >= 0.30


def test_a04_cap():
    e = [ev("like", S.ids(1)[i], S, 50 - i) for i in range(20)]
    lot = run(S, e)
    c = Counter(topics(lot))
    assert lot.shares[1] == pytest.approx(0.60) and c[1] <= 31 and len(c) >= 3


def test_a05_liked_subtopic_dominates_its_topic():
    e = [ev("like", S.ids(1, 0)[i], S, 30 - i) for i in range(3)]
    s = subs(run(S, e), S, 1)
    assert s.count(0) / len(s) >= 0.70


def test_a06_hidden_subtopic_recedes():
    lot = run(S, [ev("hide", S.ids(1, 0)[0], S, 10)])
    s = subs(lot, S, 1)
    assert 0.01 <= lot.profile.weights[1] < 0.30 and s.count(0) / len(s) <= 0.15


def test_a07_one_article_counts_once():
    a = S.ids(1)[0]
    e = [ev("view", a, S, 9, {"dwellMs": 7000}), ev("open", a, S, 8),
         ev("read", a, S, 7, {"readMs": 10**7, "scrollDepth": 1}), ev("like", a, S, 6)]
    assert sum(h.delta for h in run(S, e).profile.history) == pytest.approx(0.08)


def test_a08_like_then_unlike():
    a = S.ids(1)[0]
    p = run(S, [ev("like", a, S, 9), ev("unlike", a, S, 8)]).profile
    assert p.liked == [] and p.rejected == []
    assert p.weights[1] == pytest.approx(0.2955, abs=1e-3)   # not 0.30: renormalisation is not reversible


def test_a09_no_repeat_over_twenty_lots():
    seen, banned = set(), frozenset(S.ids(2)[:10])
    for k in range(20):
        ids = [i.article_id for i in run(S, excluded=frozenset(seen) | banned, seed=k).items]
        assert not set(ids) & (seen | banned)
        seen |= set(ids)


def test_a10_no_same_story_and_source_window():
    lot = run(S)
    c = S.catalog
    rows = [c.index[i.article_id] for i in lot.items]
    pair = sim(c.embeddings[rows], c.embeddings[rows], c.mu)
    assert pair[~np.eye(len(rows), dtype=bool)].max() < 0.80
    src = c.source_ids[rows].tolist()
    assert all(max(Counter(src[k:k + 10]).values()) <= 2 for k in range(len(src) - 9))


def test_a11_deterministic():
    e = [ev("like", S.ids(X)[0], S, 30)]
    assert run(S, e) == run(S, e)
    first = [i.article_id for i in run(S, e, seed=2).items]
    assert first != [i.article_id for i in run(S, e, seed=3).items]


def test_a12_newest_first_without_interaction():
    s = make_sim_catalog(NOW, age_hours=[1, 30, 60, 90, 120])
    lot = run(s)
    for t in CHOSEN:
        d = [s.catalog.published_at[s.catalog.index[i.article_id]]
             for i in lot.items if i.topic_id == t]
        assert d == sorted(d, reverse=True)


def test_liked_article_without_embedding_is_dropped():           # Review Focus 2
    gone = Event("e1", "like", "a_missing", NOW, NOW, {}, 1, 500)
    lot = run(S, [gone, ev("like", S.ids(1, 0)[0], S, 5)])
    assert [i.article_id for i in lot.profile.liked] == [S.ids(1, 0)[0]] and len(lot.items) == 50
