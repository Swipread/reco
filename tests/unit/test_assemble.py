import dataclasses
from collections import Counter
from collections.abc import Mapping, Sequence

from swipread_reco.allocation import TopicSequence
from swipread_reco.assemble import build_lot, candidate_mask
from swipread_reco.models import LotItem
from swipread_reco.params import Params
from swipread_reco.scoring import Scores
from swipread_reco.sim.catalog import SimCatalog, make_sim_catalog

from .helpers import NOW, near_duplicate, rows_of, scores_with, sim_from


def lot(
    sim_catalog: SimCatalog, shares: Mapping[int, float], *, count: int = 50,
    excluded: frozenset[str] = frozenset(), language: str = "fr",
    chosen: frozenset[int] = frozenset({1, 2, 3}), scores: Scores | None = None,
    offsets: Mapping[int, float] | None = None,
    liked_ids: Sequence[str] = (), rejected_ids: Sequence[str] = (),
) -> list[LotItem]:
    catalog = sim_catalog.catalog
    mask = candidate_mask(catalog, language, excluded)
    if scores is None:
        scores = scores_with(sim_catalog)
    if offsets is None:
        offsets = {t: 0.5 for t in shares}
    sequence = TopicSequence(shares, offsets)
    return build_lot(
        catalog, mask, scores, sequence, shares, chosen,
        liked_ids, rejected_ids, count, Params(),
    )


S = make_sim_catalog(NOW)
EVEN = {t: 0.1 for t in range(1, 11)}


def test_fills_count_in_sequence_order_with_no_duplicate():
    items = lot(S, EVEN)
    assert len(items) == 50 and len({i.article_id for i in items}) == 50
    assert Counter(i.topic_id for i in items) == {t: 5 for t in range(1, 11)}


def test_source_window():                                        # A10, seconde moitié
    src = [S.catalog.source_ids[S.catalog.index[i.article_id]] for i in lot(S, EVEN)]
    assert all(max(Counter(src[k:k + 10]).values()) <= 2 for k in range(41))


def test_same_story_is_never_repeated():                         # A10, première moitié
    rows = [dataclasses.replace(r, cluster_id="c1") for r in rows_of(S, S.ids(1))]
    rows += rows_of(S, S.ids(2))
    items = lot(sim_from(rows), {1: 0.5, 2: 0.5}, count=10)
    assert sum(i.topic_id == 1 for i in items) == 1              # un seul article du cluster


def test_near_duplicate_embeddings_count_as_same_story():
    rows = rows_of(S, S.ids(1)[:5])
    twin = dataclasses.replace(rows[0], id="twin")
    items = lot(sim_from(rows + [twin]), {1: 1.0}, count=6)
    assert len(items) == 5 and not {"twin", rows[0].id} <= {i.article_id for i in items}


def test_source_rule_is_relaxed_when_no_other_choice():
    rows = [dataclasses.replace(r, source_id=1) for r in rows_of(S, S.ids(1))]
    items = lot(sim_from(rows), {1: 1.0}, count=10)
    assert len(items) == 10
    assert [i.explanation["relaxedSource"] for i in items] == [False, False] + [True] * 8


def test_exhausted_topic_and_short_lot():                        # Review Focus 3
    rows = rows_of(S, S.ids(1)[:3]) + rows_of(S, S.ids(2))
    items = lot(sim_from(rows), {1: 0.5, 2: 0.5}, count=20)
    assert len(items) == 20 and sum(i.topic_id == 1 for i in items) == 3
    assert len(lot(sim_from(rows_of(S, S.ids(1)[:3])), {1: 1.0}, count=20)) == 3
    assert lot(S, EVEN, language="de") == []
    assert lot(S, {t: 0.0 for t in EVEN}) == []


def test_excluded_and_language():
    ex = frozenset(S.ids(1))
    assert all(i.topic_id != 1 for i in lot(S, EVEN, excluded=ex))


def test_reason_and_explanation_keys():
    liked = S.ids(1, 0)[:3]
    items = lot(S, EVEN, scores=scores_with(S, liked=liked), liked_ids=liked,
                excluded=frozenset(liked))
    by_topic = {i.topic_id: i for i in reversed(items)}          # première carte de chaque thème
    assert by_topic[1].reason == "similar" and len(by_topic[1].explanation["nearestLiked"]) == 3
    assert by_topic[2].reason == "onboarding" and by_topic[7].reason == "explore"
    assert set(by_topic[1].explanation) == {"affinity", "freshness", "penalty", "topicWeight",
                                            "nearestLiked", "nearestRejected", "relaxedSource"}
    assert 0.0 <= by_topic[1].score <= 1.0


def test_newest_first_without_likes():                           # A12
    s = make_sim_catalog(NOW, age_hours=[1, 30, 60, 90, 120])
    for t in (1, 2):
        dates = [s.catalog.published_at[s.catalog.index[i.article_id]]
                 for i in lot(s, EVEN) if i.topic_id == t]
        assert dates == sorted(dates, reverse=True)


def test_same_story_as_a_liked_or_rejected_article_is_never_placed():
    rows = rows_of(S, S.ids(1)[:6])
    liked, rejected = rows[0], rows[1]
    s = sim_from(rows + [near_duplicate(liked, "twin_liked"),
                         near_duplicate(rejected, "twin_rejected", seed=1)])
    scores = scores_with(s, liked=[liked.id], rejected=[rejected.id])
    assert scores.liked_sim[s.catalog.index["twin_liked"], 0] >= Params().dup_sim
    items = lot(s, {1: 1.0}, count=10, scores=scores, liked_ids=[liked.id],
                rejected_ids=[rejected.id], excluded=frozenset({liked.id, rejected.id}))
    assert sorted(i.article_id for i in items) == sorted(r.id for r in rows[2:])
