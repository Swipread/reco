"""Frozen data types shared by the recommender modules. No logic lives here."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np


@dataclass(frozen=True)
class Topic:
    id: int
    slug: str
    label: str


@dataclass(frozen=True)
class Event:
    event_id: str
    type: str                    # view, open, read, like, unlike, save, share, hide
    article_id: str
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
