"""Performance acceptance test of spec 11.2 (A13), run apart: `pytest -m perf`."""

import time

import numpy as np
import pytest

from swipread_reco.sim.catalog import make_sim_catalog

from .helpers import NOW, ev, run


@pytest.mark.perf
def test_a13_p95_under_80ms():
    s = make_sim_catalog(NOW, per_subtopic=167)                 # 5 010 articles
    ids = s.catalog.ids
    events = [ev("view", ids[i * 7], s, 400 - i, {"dwellMs": 300 + 40 * i}) for i in range(260)] + \
             [ev("like", ids[i * 11 + 3], s, 100 - i) for i in range(40)]
    times = []
    for k in range(200):
        t0 = time.perf_counter()
        run(s, events, seed=k)
        times.append(time.perf_counter() - t0)
    assert np.percentile(times, 95) < 0.080
