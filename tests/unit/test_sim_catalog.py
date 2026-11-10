from datetime import UTC, datetime

import numpy as np

from swipread_reco.scoring import sim
from swipread_reco.sim.catalog import make_sim_catalog

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)


def test_shape_and_determinism():
    a, b = make_sim_catalog(NOW), make_sim_catalog(NOW)
    assert len(a.catalog.ids) == 1800 and a.catalog.embeddings.shape == (1800, 384)
    assert np.array_equal(a.catalog.embeddings, b.catalog.embeddings)
    assert np.allclose(np.linalg.norm(a.catalog.embeddings, axis=1), 1.0, atol=1e-5)
    assert set(a.catalog.source_ids.tolist()) == set(range(1, 9))
    assert len(a.ids(3)) == 180 and len(a.ids(3, 1)) == 60


def test_geometry_separates_subtopic_topic_and_rest():
    s = make_sim_catalog(NOW); c = s.catalog
    e = lambda ids: np.stack([c.embeddings[c.index[i]] for i in ids])
    same_sub = sim(e(s.ids(1, 0)[:20]), e(s.ids(1, 0)[20:40]), c.mu).mean()
    same_topic = sim(e(s.ids(1, 0)[:20]), e(s.ids(1, 1)[:20]), c.mu).mean()
    other = sim(e(s.ids(1, 0)[:20]), e(s.ids(2, 0)[:20]), c.mu).mean()
    assert 0.55 < same_sub < 0.75 and 0.35 < same_topic < 0.55 and other < 0.1
    assert sim(e(s.ids(1, 0)), e(s.ids(1, 0)), c.mu)[~np.eye(60, dtype=bool)].max() < 0.80   # pas de fausse « même actualité »
