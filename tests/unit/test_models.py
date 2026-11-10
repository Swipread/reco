"""make_catalog edge cases."""

from swipread_reco.models import make_catalog
from swipread_reco.params import Params
from swipread_reco.recommend import RecoInput, recommend

from .helpers import NOW


def test_empty_catalog_is_representable():
    c = make_catalog([], NOW)
    assert c.ids == [] and c.embeddings.shape == (0, 0) and c.mu == 0.0
    assert c.topic_ids.shape == (0,) and c.loaded_at == NOW


def test_recommend_on_empty_catalog_gives_no_items():
    inp = RecoInput(
        request_id="r_test", language="fr", count=10, excluded=frozenset(), events=[],
        all_topics=[1, 2, 3], chosen=frozenset({1}), last_request_at=None, now=NOW,
    )
    assert recommend(inp, make_catalog([], NOW), lambda ids: {}, Params(), seed=1).items == []
