"""Allocation of feed slots between topics: share cap and interleaved order (spec section 6)."""

import hashlib
import heapq
from collections.abc import Mapping

import numpy as np

SEED_BYTES = 8  # spec 6.2: first 8 bytes of SHA-256(requestId), shifted right by one bit


def cap_shares(weights: Mapping[int, float], p_max: float) -> dict[int, float]:
    """Cap every share at p_max, spreading the excess pro rata over the others (spec 6.1).

    Repeats until no share exceeds p_max. Shares that cannot give their excess to anyone
    (all remaining topics at 0) are left uncapped. Weights themselves are not modified.
    """
    shares = {t: weights[t] for t in sorted(weights)}
    capped: set[int] = set()
    while True:
        over = [t for t in shares if t not in capped and shares[t] > p_max]
        if not over:
            return shares
        others = [t for t in shares if t not in capped and t not in over]
        mass = sum(shares[t] for t in others)
        if mass <= 0.0:
            return shares
        excess = sum(shares[t] - p_max for t in over)
        for t in over:
            shares[t] = p_max
            capped.add(t)
        for t in others:
            shares[t] += excess * shares[t] / mass


def seed_from_request_id(request_id: str) -> int:
    """Deterministic seed from the request id, in [0, 2**63) to fit a bigint (spec 6.2)."""
    digest = hashlib.sha256(request_id.encode()).digest()
    return int.from_bytes(digest[:SEED_BYTES], "big") >> 1


def topic_offsets(
    shares: Mapping[int, float], echo_topic: int | None, seed: int
) -> dict[int, float]:
    """Uniform offset u[t] in [0, 1) per topic of non-zero share; 0 for the echo topic (spec 6.2).

    One draw per such topic in increasing topic_id order, the echo topic included, so the
    other topics' offsets do not depend on which topic is the echo.
    """
    rng = np.random.default_rng(seed)
    offsets = {t: float(rng.random()) for t in sorted(shares) if shares[t] > 0.0}
    if echo_topic in offsets:
        offsets[echo_topic] = 0.0
    return offsets


class TopicSequence:
    """Topics of successive slots, by increasing virtual rank τ = (k + u) / p (spec 6.2).

    `pop` takes the next topic out of the sequence; `advance` puts it back for its next
    card. A topic popped and never advanced is exhausted. Ties go to the smallest topic_id.
    `start[t]` is the number of cards of `t` already queued before the sequence (the kept
    stock): topic `t` starts at k = start[t] instead of 0, continuing the interleaving.
    """

    def __init__(
        self, shares: Mapping[int, float], offsets: Mapping[int, float],
        start: Mapping[int, int] | None = None,
    ) -> None:
        start = start or {}
        self._shares = {t: p for t, p in shares.items() if p > 0.0 and t in offsets}
        self._offsets = {t: offsets[t] for t in self._shares}
        self._k = {t: start.get(t, 0) for t in self._shares}
        self._heap: list[tuple[float, int]] = [(self._tau(t), t) for t in self._shares]
        heapq.heapify(self._heap)
        self._popped: set[int] = set()

    def _tau(self, topic: int) -> float:
        return (self._k[topic] + self._offsets[topic]) / self._shares[topic]

    def pop(self) -> int | None:
        """Topic of the next slot, or None when every topic is exhausted."""
        if not self._heap:
            return None
        _, topic = heapq.heappop(self._heap)
        self._popped.add(topic)
        return topic

    def advance(self, topic: int) -> None:
        """Put `topic` back for its next card; call after filling a slot of `topic`."""
        if topic not in self._popped:
            raise ValueError(f"topic {topic} was not popped")
        self._popped.remove(topic)
        self._k[topic] += 1
        heapq.heappush(self._heap, (self._tau(topic), topic))
