"""Frozen data types shared by the recommender modules.

Only logic here: make_catalog, the single constructor of Catalog (computes mu).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np

from swipread_reco.scoring import baseline_mu


@dataclass(frozen=True)
class Topic:
    id: int
    slug: str
    label: str


@dataclass(frozen=True)
class Event:
    event_id: str
    type: str                    # view, open, read, like, unlike, save, unsave, share, hide
    article_id: str | None       # app.events.article_id is nullable; such events are ignored
    occurred_at: datetime
    received_at: datetime
    payload: Mapping[str, Any]   # as sent by the front end (api.md)
    topic_id: int | None         # article's main topic, None if unknown
    word_count: int | None       # app.articles.word_count, None if article unknown


@dataclass(frozen=True)
class ProfileItem:
    article_id: str
    strength: float              # strength after the age factor
    last_event_at: datetime


@dataclass(frozen=True)
class WeightChange:
    event_id: str
    type: str
    article_id: str
    topic_id: int
    delta: float                 # delta applied before renormalisation
    weight_after: float          # topic weight after renormalisation
    occurred_at: datetime


@dataclass(frozen=True)
class Profile:
    weights: dict[int, float]            # topic_id -> weight, sums to 1
    liked: list[ProfileItem]             # v > 0, most recent first
    rejected: list[ProfileItem]          # v < 0, most recent first
    echo_topic: int | None
    touched_article_ids: frozenset[str]  # every article present in the events
    history: list[WeightChange]          # chronological order


@dataclass(frozen=True)
class ProfileState:
    """What the replay of the events gives (spec 4.2 and 4.3), before the clock and the previous
    request come in: the part of the profile that `reco.user_profiles` keeps.

    `liked` and `rejected` hold the strengths before the age factor, most recent first, at most
    PROFILE_MAX_ITEMS each. `history` keeps the last weight changes only (profile.PROFILE_HISTORY_MAX).
    """

    weights: dict[int, float]            # topic_id -> weight, sums to 1
    liked: list[ProfileItem]
    rejected: list[ProfileItem]
    touched_article_ids: frozenset[str]
    history: list[WeightChange]


@dataclass(frozen=True)
class EventMark:
    """Where a profile stands in the reader's events of app.events (profile types, with article).

    `last_occurred_at` and `last_event_id` are the contract's mark: the most recent event taken
    into account. `count` also catches an event committed late with an older occurred_at.
    """

    count: int
    last_occurred_at: datetime | None
    last_event_id: str | None


@dataclass(frozen=True)
class StoredProfile:
    """A row of reco.user_profiles: the replayed state, what it was computed from and on."""

    basis: dict[str, Any]                # model version, parameters, topics, chosen topics
    mark: EventMark
    state: ProfileState


@dataclass(frozen=True)
class Catalog:
    ids: list[str]
    index: dict[str, int]                # id -> row
    topic_ids: np.ndarray                # (n,) int
    source_ids: np.ndarray               # (n,) int
    languages: list[str]                 # two-letter code
    cluster_ids: list[str | None]
    published_at: list[datetime]
    embeddings: np.ndarray               # (n, d) float32, unit norm
    mu: float
    loaded_at: datetime


@dataclass(frozen=True)
class CatalogRow:
    id: str
    topic_id: int
    source_id: int
    language: str
    cluster_id: str | None       # always None from PgStore: the backend has no cluster column
    published_at: datetime
    embedding: np.ndarray        # (d,) unit norm


def make_catalog(rows: Sequence[CatalogRow], now: datetime) -> Catalog:
    """Catalog sorted by article id, with mu computed on its embeddings (spec 5.1).

    No rows gives an empty catalog with (0, 0) embeddings.
    """
    ordered = sorted(rows, key=lambda r: r.id)
    embeddings = np.array([r.embedding for r in ordered], dtype=np.float32)
    if not ordered:
        embeddings = embeddings.reshape(0, 0)
    return Catalog(
        ids=[r.id for r in ordered],
        index={r.id: k for k, r in enumerate(ordered)},
        topic_ids=np.array([r.topic_id for r in ordered], dtype=np.int64),
        source_ids=np.array([r.source_id for r in ordered], dtype=np.int64),
        languages=[r.language for r in ordered],
        cluster_ids=[r.cluster_id for r in ordered],
        published_at=[r.published_at for r in ordered],
        embeddings=embeddings,
        mu=baseline_mu(embeddings),
        loaded_at=now,
    )


@dataclass(frozen=True)
class LotItem:
    article_id: str
    topic_id: int
    reason: str                  # similar, onboarding, explore
    score: float                 # bounded to [0, 1]
    explanation: dict[str, Any]  # keys from spec 9.1


@dataclass(frozen=True)
class Lot:
    request_id: str
    seed: int
    items: list[LotItem]
    profile: Profile
    shares: dict[int, float]     # shares after the cap
    mu: float
