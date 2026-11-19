"""In-memory test doubles of the Store and of the Redis `seen:{userId}` sets (imported explicitly)."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from swipread_reco.models import CatalogRow, Event, Lot, Topic
from swipread_reco.params import MODEL_VERSION, Params
from swipread_reco.sim.catalog import SimCatalog, make_sim_catalog
from swipread_reco.store import lot_profile_json

from .unit.helpers import rows_of

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)
WORD_COUNT = 230


class StoreDown(Exception):
    """Raised by every FakeStore method while `fail` is set."""


class FakeStore:
    """Store over a synthetic catalog (topics 1..10, 1 800 articles in French).

    Users u_1 and u_new exist (locale fr-FR, chosen topics 1, 2, 3, no events). Setting
    `fail` makes every method raise StoreDown. For the worker, `pending` lists the
    articles to encode and `fail_purge` makes purge() alone raise StoreDown.
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
        self._deleted: set[str] = set()
        # worker
        self.pending: list[tuple[str, str, str]] = []   # (id, title, summary) in app.articles
        self.written: dict[str, np.ndarray] = {}        # reco.article_embeddings: id -> embedding
        self.written_models: dict[str, str] = {}        # reco.article_embeddings: id -> model
        self.written_model: str | None = None           # model of the last write_embeddings()
        self.written_at: datetime | None = None         # `now` of the last write_embeddings()
        self.encode_calls = 0                           # articles_to_embed() calls
        self.fail_purge = False                         # purge() raises StoreDown when set
        self.purged: list[tuple[datetime, int]] = []    # (now, retention_days) of purge() calls

    def _check(self) -> None:
        if self.fail:
            raise StoreDown("store is down")

    # setup helpers

    def add_event(
        self, user_id: str, type_: str, article_id: str, *, received_at: datetime | None = None,
        occurred_at: datetime | None = None, payload: Mapping[str, Any] | None = None,
    ) -> None:
        """Event on a catalog article; each of occurred_at and received_at defaults to the other."""
        if received_at is None and occurred_at is None:
            raise ValueError("received_at or occurred_at is required")
        n = sum(len(v) for v in self.user_events.values())
        self.user_events.setdefault(user_id, []).append(Event(
            event_id=f"e_{n}", type=type_, article_id=article_id,
            occurred_at=occurred_at or received_at, received_at=received_at or occurred_at,
            payload=dict(payload or {}), topic_id=self.sim.subtopic[article_id][0],
            word_count=WORD_COUNT,
        ))

    def drop_articles(self, ids: Sequence[str]) -> None:
        """Leave `ids` out of the next catalog_rows()."""
        self._dropped.update(ids)

    def delete_articles(self, ids: Sequence[str]) -> None:
        """Remove `ids` from app.articles: no title, no embedding any more."""
        self._deleted.update(ids)

    def _exists(self, article_id: str) -> bool:
        return article_id in self.sim.catalog.index and article_id not in self._deleted

    def _in_app(self, article_id: str) -> bool:
        """In app.articles: a catalog article or a pending one, not deleted."""
        listed = article_id in self.sim.catalog.index or any(p[0] == article_id for p in self.pending)
        return listed and article_id not in self._deleted

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
        with_article = [e for e in self.user_events.get(user_id, []) if e.article_id is not None]
        ordered = sorted(with_article, key=lambda e: e.occurred_at)
        return ordered[-limit:] if limit > 0 else []

    def user_article_ids(self, user_id: str) -> frozenset[str]:
        self._check()
        return frozenset(self.user_articles.get(user_id, set()))

    def embeddings(self, ids: Sequence[str]) -> dict[str, np.ndarray]:
        self._check()
        return self.sim.embeddings([i for i in ids if self._exists(i)])

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
        """Same shape as PgStore.request: snake_case keys, profile/params/explanation as JSON."""
        self._check()
        saved = self.lots.get(request_id)
        if saved is None:
            return None
        lot: Lot = saved["lot"]
        slugs = {t.id: t.slug for t in self.topic_list}
        titles = self.article_titles([i.article_id for i in lot.items])
        return {
            "request_id": lot.request_id, "user_id": saved["user_id"],
            "session_id": saved["session_id"], "model_version": MODEL_VERSION,
            "requested_count": saved["requested_count"], "seed": lot.seed,
            "profile": lot_profile_json(lot, slugs), "params": saved["params"].as_dict(),
            "generated_at": saved["generated_at"],
            "items": [
                {
                    "position": position, "article_id": item.article_id,
                    "topic_id": item.topic_id, "reason": item.reason, "score": item.score,
                    "explanation": item.explanation, "title": titles.get(item.article_id),
                }
                for position, item in enumerate(lot.items)
            ],
        }

    def article_titles(self, ids: Sequence[str]) -> dict[str, str]:
        self._check()
        return {i: f"Title {i}" for i in ids if self._exists(i)}

    def articles_to_embed(self, model: str, limit: int) -> list[tuple[str, str, str]]:
        """Pending articles without a `model` embedding (none, or one of another model)."""
        self._check()
        self.encode_calls += 1
        return [
            p for p in self.pending
            if self.written_models.get(p[0]) != model and p[0] not in self._deleted
        ][:limit]

    def write_embeddings(
        self, rows: Sequence[tuple[str, np.ndarray]], model: str, now: datetime
    ) -> None:
        """Upsert: an article keeps one embedding, of the last model written."""
        self._check()
        for article_id, embedding in rows:
            self.written[article_id] = embedding
            self.written_models[article_id] = model
        self.written_model = model
        self.written_at = now

    def fallback_ids(
        self, topic_id: int, now: datetime, max_age_days: int, limit: int
    ) -> list[str]:
        """Same order as PgStore: published_at descending, then id."""
        rows = [r for r in self.catalog_rows(now, max_age_days) if r.topic_id == topic_id]
        rows.sort(key=lambda r: r.id)
        rows.sort(key=lambda r: r.published_at, reverse=True)
        return [r.id for r in rows[:limit]]

    def purge(self, now: datetime, retention_days: int) -> int:
        """Delete saved lots older than `retention_days` or of users gone from `locales`, and
        written embeddings of articles gone from app.articles; the number of lots deleted."""
        self._check()
        if self.fail_purge:
            raise StoreDown("purge failed")
        self.purged.append((now, retention_days))
        oldest = now - timedelta(days=retention_days)
        gone = [
            request_id for request_id, saved in self.lots.items()
            if saved["generated_at"] < oldest or saved["user_id"] not in self.locales
        ]
        for request_id in gone:
            del self.lots[request_id]
        for article_id in [i for i in self.written if not self._in_app(i)]:
            del self.written[article_id]
            del self.written_models[article_id]
        return len(gone)


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
