"""The pure recommendation pipeline: profile, scoring, allocation, assembly (spec 2 and 7.1)."""

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from swipread_reco.allocation import TopicSequence, cap_shares, seed_from_request_id, topic_offsets
from swipread_reco.assemble import build_lot, candidate_mask
from swipread_reco.models import Catalog, Event, Lot, ProfileItem
from swipread_reco.params import Params
from swipread_reco.profile import build_profile
from swipread_reco.scoring import score_candidates

FetchEmbeddings = Callable[[Sequence[str]], Mapping[str, np.ndarray]]


@dataclass(frozen=True)
class RecoInput:
    request_id: str
    language: str                    # two letters, lower case
    count: int
    excluded: frozenset[str]         # exclude ∪ seen:{userId} ∪ app.user_articles
    events: Sequence[Event]
    all_topics: Sequence[int]
    chosen: frozenset[int]
    last_request_at: datetime | None
    now: datetime
    stock: frozenset[str] = frozenset()  # the request's `exclude` only: the stock the backend kept


def _with_embeddings(
    items: list[ProfileItem], found: Mapping[str, np.ndarray], dim: int
) -> tuple[list[ProfileItem], np.ndarray, np.ndarray]:
    """Items that have an embedding (spec 4.3), with their (k, dim) embeddings and strengths."""
    kept = [item for item in items if item.article_id in found]
    if not kept:
        return [], np.zeros((0, dim), dtype=np.float32), np.zeros(0)
    emb = np.stack([found[item.article_id] for item in kept]).astype(np.float32)
    return kept, emb, np.array([item.strength for item in kept], dtype=np.float64)


def _echo_after_stock(
    echo_topic: int | None, stock: frozenset[str], catalog: Catalog
) -> int | None:
    """No echo when the kept stock already holds an article of the echo topic (spec 4.4).

    Stock ids absent from the catalog are ignored.
    """
    if echo_topic is None:
        return None
    for article_id in stock:
        k = catalog.index.get(article_id)
        if k is not None and int(catalog.topic_ids[k]) == echo_topic:
            return None
    return echo_topic


def recommend(
    inp: RecoInput,
    catalog: Catalog,
    fetch_embeddings: FetchEmbeddings,
    params: Params,
    seed: int | None = None,
) -> Lot:
    """Build the reader's lot from their events and the catalog, without I/O but `fetch_embeddings`.

    `fetch_embeddings` returns the embeddings it knows among the given ids; liked and rejected
    articles it leaves out are dropped from the profile. `seed` None derives the seed from
    the request id (spec 6.2).
    """
    if seed is None:
        seed = seed_from_request_id(inp.request_id)

    profile = build_profile(
        inp.events, inp.all_topics, inp.chosen, inp.now, inp.last_request_at, params
    )
    wanted = [item.article_id for item in profile.liked + profile.rejected]
    found = fetch_embeddings(wanted) if wanted else {}
    dim = catalog.embeddings.shape[1]
    liked, liked_emb, liked_strength = _with_embeddings(profile.liked, found, dim)
    rejected, rejected_emb, rejected_strength = _with_embeddings(profile.rejected, found, dim)
    profile = dataclasses.replace(
        profile, liked=liked, rejected=rejected,
        echo_topic=_echo_after_stock(profile.echo_topic, inp.stock, catalog),
    )

    scores = score_candidates(
        catalog.embeddings, catalog.published_at,
        liked_emb, liked_strength, rejected_emb, rejected_strength,
        catalog.mu, inp.now, params,
    )
    shares = cap_shares(profile.weights, params.p_max)
    offsets = topic_offsets(shares, profile.echo_topic, seed)
    mask = candidate_mask(catalog, inp.language, inp.excluded | profile.touched_article_ids)
    items = build_lot(
        catalog, mask, scores, TopicSequence(shares, offsets), profile.weights, inp.chosen,
        [item.article_id for item in liked], [item.article_id for item in rejected],
        inp.count, params,
    )
    return Lot(
        request_id=inp.request_id,
        seed=seed,
        items=items,
        profile=profile,
        shares=shares,
        mu=catalog.mu,
    )
