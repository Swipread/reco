"""Shared helpers for the acceptance tests of spec 11.2 (imported explicitly)."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from swipread_reco.models import Event, Lot
from swipread_reco.params import Params
from swipread_reco.recommend import RecoInput, recommend
from swipread_reco.sim.catalog import SimCatalog

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)
CHOSEN = frozenset({1, 2, 3})
X, Y = 4, 5                                    # unchosen topics
ALL_TOPICS = tuple(range(1, 11))
WORD_COUNT = 230
DEFAULT_PARAMS = Params()


def ev(
    type: str, article_id: str, sim: SimCatalog, minutes_ago: float,
    payload: Mapping[str, Any] | None = None,
) -> Event:
    """topic_id read from sim.subtopic, word_count 230, received_at = occurred_at."""
    at = NOW - timedelta(minutes=minutes_ago)
    return Event(
        event_id=f"e_{type}_{article_id}_{minutes_ago}",
        type=type,
        article_id=article_id,
        occurred_at=at,
        received_at=at,
        payload=dict(payload or {}),
        topic_id=sim.subtopic[article_id][0],
        word_count=WORD_COUNT,
    )


def _fetch(sim: SimCatalog):
    """Embeddings of the ids known to the synthetic catalog; unknown ids are left out."""
    index = sim.catalog.index

    def fetch(ids: Sequence[str]) -> dict[str, np.ndarray]:
        return sim.embeddings([i for i in ids if i in index])

    return fetch


def run(
    sim: SimCatalog, events: Sequence[Event] = (), *, count: int = 50, seed: int = 1,
    excluded: frozenset[str] = frozenset(), stock: Sequence[str] = (),
    chosen: frozenset[int] = CHOSEN,
    last_request_at: datetime | None = NOW - timedelta(hours=1), now: datetime = NOW,
    params: Params = DEFAULT_PARAMS,
) -> Lot:
    """Call recommend with request_id="r_test", language="fr", all_topics=1..10.

    `stock` is the kept stock sent as `exclude`, in order; like the backend, it is also excluded.
    """
    inp = RecoInput(
        request_id="r_test",
        language="fr",
        count=count,
        excluded=excluded | frozenset(stock),
        events=events,
        all_topics=ALL_TOPICS,
        chosen=chosen,
        last_request_at=last_request_at,
        now=now,
        stock=tuple(stock),
    )
    return recommend(inp, sim.catalog, _fetch(sim), params, seed=seed)


def topics(lot: Lot) -> list[int]:
    """Topic of each card, in lot order."""
    return [i.topic_id for i in lot.items]


def subs(lot: Lot, sim: SimCatalog, topic: int) -> list[int]:
    """Subtopic of each card of `topic`, in lot order."""
    return [sim.subtopic[i.article_id][1] for i in lot.items if i.topic_id == topic]
