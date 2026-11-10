from datetime import UTC, datetime, timedelta
from math import sqrt

import numpy as np
import pytest

from swipread_reco.params import Params
from swipread_reco.scoring import baseline_mu, score_candidates, sim

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)


def u(c: float) -> np.ndarray:
    """2-d unit vector whose cosine with (1, 0) is c."""
    return np.array([c, sqrt(1 - c * c)], dtype=np.float32)


A = u(1.0)[None, :]
EMPTY = np.zeros((0, 2), np.float32)


def test_sim_recenters_and_clips():
    assert sim(A, np.stack([u(0.9), u(0.5), u(1.0)]), 0.8)[0] == pytest.approx([0.5, 0.0, 1.0], abs=1e-6)


def test_mu_is_zero_under_50_rows_and_deterministic_above():
    rng = np.random.default_rng(1)
    e = rng.normal(size=(200, 16)).astype(np.float32)
    e /= np.linalg.norm(e, axis=1, keepdims=True)
    assert baseline_mu(e[:49]) == 0.0
    assert baseline_mu(e) == baseline_mu(e) and abs(baseline_mu(e)) < 0.1


def test_affinity_is_mean_of_top_three():
    liked = np.stack([u(0.9), u(0.8), u(0.7), u(0.1)])
    s = score_candidates(A, [NOW], liked, np.ones(4), EMPTY, np.zeros(0), 0.0, NOW, Params())
    assert s.affinity[0] == pytest.approx(0.8, abs=1e-6)
    s = score_candidates(A, [NOW], liked, np.array([0.5, 1, 1, 1]), EMPTY, np.zeros(0), 0.0, NOW, Params())
    assert s.affinity[0] == pytest.approx(0.65, abs=1e-6)


def test_affinity_with_one_or_zero_liked():
    one = score_candidates(A, [NOW], u(0.6)[None, :], np.ones(1), EMPTY, np.zeros(0), 0.0, NOW, Params())
    none = score_candidates(A, [NOW], EMPTY, np.zeros(0), EMPTY, np.zeros(0), 0.0, NOW, Params())
    assert one.affinity[0] == pytest.approx(0.6, abs=1e-6) and none.affinity[0] == 0.0


def test_freshness_halves_every_48h_and_caps_future_dates():
    dates = [NOW, NOW - timedelta(hours=48), NOW - timedelta(hours=96), NOW + timedelta(hours=5)]
    s = score_candidates(np.repeat(A, 4, 0), dates, EMPTY, np.zeros(0), EMPTY, np.zeros(0), 0.0, NOW, Params())
    assert s.freshness == pytest.approx([1.0, 0.5, 0.25, 1.0])


def test_raw_and_clipped_score():
    two_days = [NOW - timedelta(hours=48)]
    s = score_candidates(A, two_days, u(0.6)[None, :], np.ones(1), EMPTY, np.zeros(0), 0.0, NOW, Params())
    assert s.raw[0] == pytest.approx(0.56, abs=1e-6)                     # 0,6×0,6 + 0,4×0,5
    s = score_candidates(A, two_days, u(0.6)[None, :], np.ones(1), A, np.ones(1), 0.0, NOW, Params())
    assert s.penalty[0] == pytest.approx(1.0) and s.raw[0] == pytest.approx(0.06, abs=1e-6)
    four_days = [NOW - timedelta(hours=96)]
    s = score_candidates(A, four_days, EMPTY, np.zeros(0), A, np.ones(1), 0.0, NOW, Params())
    assert s.raw[0] == pytest.approx(-0.4, abs=1e-6) and s.score[0] == 0.0  # classé sur raw, renvoyé borné


def test_penalty_is_max_of_weighted_sims():
    rej = np.stack([u(0.9), u(0.5)])
    s = score_candidates(A, [NOW], EMPTY, np.zeros(0), rej, np.array([0.15, 1.0]), 0.0, NOW, Params())
    assert s.penalty[0] == pytest.approx(0.5, abs=1e-6)
