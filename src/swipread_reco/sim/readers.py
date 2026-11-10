"""Simulated readers with a fixed rule for each card (spec 11.3)."""

from dataclasses import dataclass
from typing import Any

from swipread_reco.sim.catalog import SimCatalog

LONG_DWELL_MS = 8000    # view long enough to count as a long view
SHORT_DWELL_MS = 800    # view short enough to count as a skip


@dataclass(frozen=True)
class Reader:
    name: str
    chosen: frozenset[int]
    loves: frozenset[tuple[int, int]]     # (topic, subtopic) -> 8 s view then like
    likes_topics: frozenset[int]          # 8 s view, no like
    hates_topics: frozenset[int]          # hide
    # anything else: 800 ms view (skipped)

    def react(self, article_id: str, sim: SimCatalog) -> list[tuple[str, dict[str, Any]]]:
        """Events (type, payload) the reader sends for this card, in order."""
        topic, sub = sim.subtopic[article_id]
        if (topic, sub) in self.loves:
            return [("view", {"dwellMs": LONG_DWELL_MS}), ("like", {})]
        if topic in self.hates_topics:
            return [("hide", {})]
        if topic in self.likes_topics:
            return [("view", {"dwellMs": LONG_DWELL_MS})]
        return [("view", {"dwellMs": SHORT_DWELL_MS})]

    def relevant(self, article_id: str, sim: SimCatalog) -> bool:
        """True if the card matches the reader's tastes (loved subtopic or liked topic)."""
        topic, sub = sim.subtopic[article_id]
        return (topic, sub) in self.loves or topic in self.likes_topics


READERS: list[Reader] = [
    Reader(
        name="fidele",
        chosen=frozenset({1, 2, 3}),
        loves=frozenset({(1, 0)}),
        likes_topics=frozenset({1, 2, 3}),
        hates_topics=frozenset(),
    ),
    Reader(
        name="passionne",
        chosen=frozenset({1, 2, 3}),
        loves=frozenset({(4, 0), (4, 1), (4, 2)}),
        likes_topics=frozenset({1, 2, 3}),
        hates_topics=frozenset(),
    ),
    Reader(
        name="allergique",
        chosen=frozenset({1, 2, 3}),
        loves=frozenset(),
        likes_topics=frozenset({1, 2}),
        hates_topics=frozenset({3}),
    ),
    Reader(
        name="zappeur",
        chosen=frozenset({1, 2, 3}),
        loves=frozenset(),
        likes_topics=frozenset(),
        hates_topics=frozenset(),
    ),
]
