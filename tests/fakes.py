"""In-memory test doubles of the Store and of the Redis `seen:{userId}` sets (imported explicitly)."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from swipread_reco.models import CatalogRow, Event, Lot, Topic
from swipread_reco.params import Params
from swipread_reco.sim.catalog import SimCatalog, make_sim_catalog

from .unit.helpers import rows_of

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)
WORD_COUNT = 230


class StoreDown(Exception):
    """Raised by every FakeStore method while `fail` is set."""


class FakeStore:
    """Store over a synthetic catalog (topics 1..10, 1 800 articles in French).

    Users u_1 and u_new exist (locale fr-FR, chosen topics 1, 2, 3, no events). Setting
    `fail` makes every method raise StoreDown.
    """

    def __init__(self, sim: SimCatalog | None = None) -> None:
        self.sim = sim if sim is not None else make_sim_catalog(NOW)
        self.fail = False
        self.topic_list = [Topic(id=t, slug=f"t{t}", label=f"Topic {t}") for t in range(1, 11)]
        self.locales: dict[str, str] = {"u_1": "fr-FR", "u_new": "fr-FR"}
        self.chosen: dict[str, frozenset[int]] = {u: frozenset({1, 2, 3}) for u in self.locales}
        self.user_events: dict[str, list[Event]] = {}
        self.user_articles: dict[str, set[str]] = {}
        self.lots: dict[str, dict[str, Any]] = {}     # request_id -> save_lot arguments
        self.events_limit: int | None = None          # `limit` of the last events() call
        self._dropped: set[str] = set()

    def _check(self) -> None:
        if self.fail:
            raise StoreDown("store is down")

    # setup helpers

    def add_event(
        self, user_id: str, type_: str, article_id: str, *, received_at: datetime,
        occurred_at: datetime | None = None, payload: Mapping[str, Any] | None = None,
    ) -> None:
        """Event on a catalog article; occurred_at defaults to received_at."""
        n = sum(len(v) for v in self.user_events.values())
        self.user_events.setdefault(user_id, []).append(Event(
            event_id=f"e_{n}", type=type_, article_id=article_id,
            occurred_at=occurred_at or received_at, received_at=received_at,
            payload=dict(payload or {}), topic_id=self.sim.subtopic[article_id][0],
            word_count=WORD_COUNT,
        ))

    def drop_articles(self, ids: Sequence[str]) -> None:
        """Leave `ids` out of the next catalog_rows()."""
        self._dropped.update(ids)

    # Store

    def ensure_schema(self) -> None:
        self._check()

    def topics(self) -> list[Topic]:
        self._check()
        return list(self.topic_list)

    def user_locale(self, user_id: str) -> str | None:
        self._check()
        return self.locales.get(user_id)

    def chosen_topics(self, user_id: str) -> frozenset[int]:
        self._check()
        return self.chosen.get(user_id, frozenset())

    def events(self, user_id: str, limit: int) -> list[Event]:
        self._check()
        self.events_limit = limit
        ordered = sorted(self.user_events.get(user_id, []), key=lambda e: e.occurred_at)
        return ordered[-limit:] if limit > 0 else []

    def user_article_ids(self, user_id: str) -> frozenset[str]:
        self._check()
        return frozenset(self.user_articles.get(user_id, set()))

    def embeddings(self, ids: Sequence[str]) -> dict[str, np.ndarray]:
        self._check()
        return self.sim.embeddings([i for i in ids if i in self.sim.catalog.index])

    def last_request_at(self, user_id: str) -> datetime | None:
        self._check()
        times = [s["generated_at"] for s in self.lots.values() if s["user_id"] == user_id]
        return max(times, default=None)

    def catalog_rows(self, now: datetime, max_age_days: int) -> list[CatalogRow]:
        self._check()
        oldest = now - timedelta(days=max_age_days)
        ids = [i for i in self.sim.catalog.ids if i not in self._dropped]
        return [r for r in rows_of(self.sim, ids) if r.published_at >= oldest]

    def save_lot(
        self, lot: Lot, *, user_id: str, session_id: str | None,
        requested_count: int, generated_at: datetime, params: Params,
    ) -> None:
        self._check()
        self.lots[lot.request_id] = {
            "lot": lot, "user_id": user_id, "session_id": session_id,
            "requested_count": requested_count, "generated_at": generated_at, "params": params,
        }

    def request(self, request_id: str) -> dict[str, Any] | None:
        raise NotImplementedError("not needed yet")

    def article_titles(self, ids: Sequence[str]) -> dict[str, str]:
        self._check()
        return {i: f"Title {i}" for i in ids if i in self.sim.catalog.index}

    def articles_to_embed(self, model: str, limit: int) -> list[tuple[str, str, str]]:
        raise NotImplementedError("not needed yet")

    def write_embeddings(
        self, rows: Sequence[tuple[str, np.ndarray]], model: str, now: datetime
    ) -> None:
        raise NotImplementedError("not needed yet")

    def fallback_ids(
        self, topic_id: int, now: datetime, max_age_days: int, limit: int
    ) -> list[str]:
        raise NotImplementedError("not needed yet")

    def purge(self, now: datetime, retention_days: int) -> int:
        raise NotImplementedError("not needed yet")


class SeenDown(Exception):
    """Raised by FakeSeen.seen while `fail` is set."""


class FakeSeen:
    """The Redis `seen:{userId}` sets, in memory; `fail` makes seen() raise SeenDown."""

    def __init__(self) -> None:
        self.fail = False
        self.sets: dict[str, set[str]] = {}

    def add(self, user_id: str, *article_ids: str) -> None:
        self.sets.setdefault(user_id, set()).update(article_ids)

    def seen(self, user_id: str) -> frozenset[str]:
        if self.fail:
            raise SeenDown("redis is down")
        return frozenset(self.sets.get(user_id, set()))
