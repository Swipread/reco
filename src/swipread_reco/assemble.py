"""Lot assembly: fill each slot with the best article of its topic (spec section 7)."""

from collections import Counter, deque
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import Any

import numpy as np

from swipread_reco.allocation import TopicSequence
from swipread_reco.models import Catalog, LotItem
from swipread_reco.params import Params
from swipread_reco.scoring import Scores, sim

# Definitions fixed by spec 7.2 and 9.1 (not tunables of section 10).
SOURCE_WINDOW = 10      # SOURCE_MAX articles at most per window of 10 consecutive cards
NEAREST_LIKED_MAX = 3   # explanation.nearestLiked keeps the 3 closest liked articles


def candidate_mask(catalog: Catalog, language: str, excluded: AbstractSet[str]) -> np.ndarray:
    """Rows in the reader's language and not excluded (spec 7.1), as an (n,) bool array."""
    return np.array(
        [lang == language and i not in excluded
         for i, lang in zip(catalog.ids, catalog.languages, strict=True)],
        dtype=bool,
    )


def _ranked_by_topic(catalog: Catalog, mask: np.ndarray, scores: Scores) -> dict[int, list[int]]:
    """Candidate rows per topic, best first: raw desc, published_at desc, id asc (spec 5.3).

    Catalog rows are sorted by id, so the row index is the id tie-break.
    """
    rows = np.flatnonzero(mask)
    published = np.array([catalog.published_at[r].timestamp() for r in rows], dtype=np.float64)
    order = rows[np.lexsort((rows, -published, -scores.raw[rows]))]
    ranked: dict[int, list[int]] = {}
    for r in order.tolist():
        ranked.setdefault(int(catalog.topic_ids[r]), []).append(r)
    return ranked


def _nearest(similarities: np.ndarray, ids: Sequence[str], k: int) -> list[dict[str, Any]]:
    """Up to k `{articleId, sim}` of positive sim, sim desc (ties keep the input order)."""
    order = np.argsort(-similarities, kind="stable")[:k]
    return [
        {"articleId": ids[j], "sim": float(similarities[j])}
        for j in order.tolist() if similarities[j] > 0.0
    ]


def _reason(affinity: float, topic: int, chosen: AbstractSet[int], params: Params) -> str:
    """`reason` field of a card (spec 7.3)."""
    if affinity >= params.aff_reason_min:
        return "similar"
    if topic in chosen:
        return "onboarding"
    return "explore"


def build_lot(
    catalog: Catalog,
    mask: np.ndarray,
    scores: Scores,
    sequence: TopicSequence,
    weights: Mapping[int, float],
    chosen: AbstractSet[int],
    liked_ids: Sequence[str],
    rejected_ids: Sequence[str],
    count: int,
    params: Params,
    stock_rows: Sequence[int] = (),
) -> list[LotItem]:
    """Fill up to `count` slots in the order of `sequence` (spec 7.2).

    For each slot, the best-ranked remaining candidate of its topic that is not the same
    story as a placed article (rule 1) and keeps sources varied (rule 2); failing that,
    rule 1 alone (relaxedSource). A topic with no such candidate is exhausted.
    `scores` covers every catalog row; `mask` selects the candidates.

    Articles the reader liked or rejected count as placed for rule 1 (similarity only:
    `scores.liked_sim` and `scores.rejected_sim`). `stock_rows`, the catalog rows of the
    kept stock in stock order, count as placed before position 0 for rules 1 and 2.
    """
    ranked = _ranked_by_topic(catalog, mask, scores)
    candidates = np.flatnonzero(mask)
    candidate_emb = catalog.embeddings[candidates]
    cluster_rows: dict[str, list[int]] = {}
    for r in candidates.tolist():
        if catalog.cluster_ids[r] is not None:
            cluster_rows.setdefault(catalog.cluster_ids[r], []).append(r)
    # rows excluded by rule 1, starting with the stories the reader already reacted to
    same_story = ((scores.liked_sim >= params.dup_sim).any(axis=1)
                  | (scores.rejected_sim >= params.dup_sim).any(axis=1))
    recent_sources: deque[int] = deque(maxlen=SOURCE_WINDOW - 1)

    def place(rows: Sequence[int]) -> None:
        """Mark the same stories as `rows` (rule 1) and push their sources (rule 2)."""
        rows = list(rows)
        dup = sim(catalog.embeddings[rows], candidate_emb, catalog.mu) >= params.dup_sim
        same_story[candidates[dup.any(axis=0)]] = True
        same_story[rows] = True
        for r in rows:
            same_story[cluster_rows.get(catalog.cluster_ids[r], [])] = True
            recent_sources.append(int(catalog.source_ids[r]))

    if stock_rows:
        place(stock_rows)
    items: list[LotItem] = []

    while len(items) < count:
        topic = sequence.pop()
        if topic is None:
            break
        remaining = [r for r in ranked.get(topic, []) if not same_story[r]]
        ranked[topic] = remaining
        if not remaining:
            continue                                       # topic exhausted: never advanced
        source_counts = Counter(recent_sources)
        row = next(
            (r for r in remaining
             if source_counts[int(catalog.source_ids[r])] <= params.source_max - 1),
            None,
        )
        relaxed = row is None
        if row is None:
            row = remaining[0]

        place([row])

        affinity = float(scores.affinity[row])
        rejected = _nearest(scores.rejected_sim[row], rejected_ids, 1)
        items.append(LotItem(
            article_id=catalog.ids[row],
            topic_id=topic,
            reason=_reason(affinity, topic, chosen, params),
            score=float(scores.score[row]),
            explanation={
                "affinity": affinity,
                "freshness": float(scores.freshness[row]),
                "penalty": float(scores.penalty[row]),
                "topicWeight": float(weights.get(topic, 0.0)),
                "nearestLiked": _nearest(scores.liked_sim[row], liked_ids, NEAREST_LIKED_MAX),
                "nearestRejected": rejected[0] if rejected else None,
                "relaxedSource": relaxed,
            },
        ))
        sequence.advance(topic)
    return items
