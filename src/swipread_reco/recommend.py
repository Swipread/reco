"""The pure recommendation pipeline: profile, scoring, allocation, assembly (spec 2 and 7.1)."""

import dataclasses
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from swipread_reco.allocation import TopicSequence, cap_shares, seed_from_request_id, topic_offsets
from swipread_reco.assemble import build_lot, candidate_mask
from swipread_reco.models import Catalog, Event, Lot, ProfileItem, ProfileState
from swipread_reco.params import Params
from swipread_reco.profile import echo_candidate, finish_profile, replay_profile
from swipread_reco.scoring import score_candidates

FetchEmbeddings = Callable[[Sequence[str]], Mapping[str, np.ndarray]]


@dataclass(frozen=True)
class RecoInput:
    request_id: str
    language: str                    # two letters, lower case
    count: int
    excluded: frozenset[str]         # exclude ∪ seen:{userId} ∪ app.user_articles
    events: Sequence[Event]          # all of them, or only those received after last_request_at with `state`
    all_topics: Sequence[int]
    chosen: frozenset[int]
    last_request_at: datetime | None
    now: datetime
    stock: tuple[str, ...] = ()      # the request's `exclude` only, in order: the stock the backend kept
    state: ProfileState | None = None  # replay already done (reco.user_profiles); None: replay `events`


def _with_embeddings(
    items: list[ProfileItem], found: Mapping[str, np.ndarray], dim: int
) -> tuple[list[ProfileItem], np.ndarray, np.ndarray]:
    """Items that have an embedding (spec 4.3), with their (k, dim) embeddings and strengths."""
    kept = [item for item in items if item.article_id in found]
    if not kept:
        return [], np.zeros((0, dim), dtype=np.float32), np.zeros(0)
    emb = np.stack([found[item.article_id] for item in kept]).astype(np.float32)
    return kept, emb, np.array([item.strength for item in kept], dtype=np.float64)


def _stock_rows(stock: Sequence[str], catalog: Catalog) -> list[int]:
    """Catalog rows of the kept stock, in stock order; unknown and repeated ids are ignored."""
    rows: dict[int, None] = {}
    for article_id in stock:
        k = catalog.index.get(article_id)
        if k is not None:
            rows.setdefault(k, None)
    return list(rows)


def _echo_after_stock(
    echo_topic: int | None, stock_topics: Counter[int]
) -> int | None:
    """No echo when the kept stock already holds an article of the echo topic (spec 4.4)."""
    if echo_topic is None or stock_topics[echo_topic] > 0:
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

    state = inp.state
    if state is None:
        state = replay_profile(inp.events, inp.all_topics, inp.chosen, params)
    profile = finish_profile(state, echo_candidate(inp.events, inp.last_request_at), inp.now, params)
    wanted = [item.article_id for item in profile.liked + profile.rejected]
    found = fetch_embeddings(wanted) if wanted else {}
    dim = catalog.embeddings.shape[1]
    liked, liked_emb, liked_strength = _with_embeddings(profile.liked, found, dim)
    rejected, rejected_emb, rejected_strength = _with_embeddings(profile.rejected, found, dim)
    stock_rows = _stock_rows(inp.stock, catalog)
    stock_topics = Counter(int(catalog.topic_ids[r]) for r in stock_rows)
    profile = dataclasses.replace(
        profile, liked=liked, rejected=rejected,
        echo_topic=_echo_after_stock(profile.echo_topic, stock_topics),
    )

    scores = score_candidates(
        catalog.embeddings, catalog.published_at,
        liked_emb, liked_strength, rejected_emb, rejected_strength,
        catalog.mu, inp.now, params,
    )
    shares = cap_shares(profile.weights, params.p_max)
    offsets = topic_offsets(shares, profile.echo_topic, seed)
    mask = candidate_mask(catalog, inp.language, inp.excluded | profile.touched_article_ids)
    # The kept stock is queued before this lot: its cards count as already placed (spec 6.2, 7.2).
    items = build_lot(
        catalog, mask, scores, TopicSequence(shares, offsets, start=stock_topics),
        profile.weights, inp.chosen,
        [item.article_id for item in liked], [item.article_id for item in rejected],
        inp.count, params, stock_rows,
    )
    return Lot(
        request_id=inp.request_id,
        seed=seed,
        items=items,
        profile=profile,
        shares=shares,
        mu=catalog.mu,
    )
