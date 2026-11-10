"""Reader profile built from interaction events (spec section 4)."""

import dataclasses
import math
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from swipread_reco.models import Event, Profile, ProfileItem, WeightChange
from swipread_reco.params import Params

# Definitions fixed by spec 4.1 (not tunables of section 10): expected reading time
# is word_count / READING_WPM minutes, or FALLBACK_READ_MS when word_count is 0 or unknown.
READING_WPM = 230
MS_PER_MINUTE = 60_000
FALLBACK_READ_MS = 60_000

# Definitions fixed by spec 4.4 and the backend contract (not tunables of section 10):
# the strong positive events that make a topic the echo topic.
ECHO_TYPES = frozenset({"like", "save", "share"})
ECHO_READ_SCROLL_DEPTH = 0.8   # a `read` is strong only with scrollDepth strictly above


@dataclass(frozen=True)
class ArticleState:
    skipped: bool = False
    long_view: bool = False
    opened: bool = False
    read: float | None = None    # None = never read; otherwise max of the read ratios c
    liked: bool = False
    saved: bool = False
    shared: bool = False
    hidden: bool = False


def _payload_number(payload: Any, key: str) -> float | None:
    """Return payload[key] if it is a finite, non-negative number, else None."""
    try:
        raw = payload.get(key)
    except AttributeError:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    if not math.isfinite(raw) or raw < 0:
        return None
    return float(raw)


def _expected_read_ms(word_count: int | None) -> float:
    if not word_count or word_count <= 0:
        return FALLBACK_READ_MS
    return word_count / READING_WPM * MS_PER_MINUTE


def apply_event(state: ArticleState, event: Event, params: Params) -> ArticleState:
    """Fold one event into the article state (spec 4.1 table).

    Unknown event types and events with a malformed payload leave the state unchanged.
    """
    match event.type:
        case "view":
            dwell = _payload_number(event.payload, "dwellMs")
            if dwell is None:
                return state
            if dwell < params.t_skip_ms:
                return dataclasses.replace(state, skipped=True)
            if dwell >= params.t_long_ms:
                return dataclasses.replace(state, long_view=True)
            return state
        case "open":
            return dataclasses.replace(state, opened=True)
        case "read":
            read_ms = _payload_number(event.payload, "readMs")
            if read_ms is None:
                return state
            c = min(1.0, read_ms / _expected_read_ms(event.word_count))
            return dataclasses.replace(state, read=max(state.read or 0.0, c))
        case "like":
            return dataclasses.replace(state, liked=True)
        case "unlike":
            return dataclasses.replace(state, liked=False)
        case "save":
            return dataclasses.replace(state, saved=True)
        case "share":
            return dataclasses.replace(state, shared=True)
        case "hide":
            return dataclasses.replace(state, hidden=True)
        case _:
            return state


def article_value(state: ArticleState, params: Params) -> float:
    """Value v of the article for the reader: its strongest signal only (spec 4.1)."""
    if state.hidden:
        return params.v_hide
    candidates = [0.0]
    if state.long_view:
        candidates.append(params.v_long_view)
    if state.opened:
        candidates.append(params.v_open)
    if state.read is not None:
        candidates.append(params.v_open + params.v_read_bonus * state.read)
    if state.liked or state.saved or state.shared:
        candidates.append(params.v_like)
    pos = max(candidates)
    if pos > 0:
        return pos
    if state.skipped:
        return params.v_skip
    return 0.0


def initial_weights(all_topics: Sequence[int], chosen: AbstractSet[int], params: Params) -> dict[int, float]:
    """Starting topic weights (spec 4.2): EPSILON shared by the unchosen topics.

    An empty topic list gives empty weights.
    """
    topics = list(dict.fromkeys(all_topics))
    k = len(topics)
    if k == 0:
        return {}
    c = sum(1 for t in topics if t in chosen)
    if c == 0 or c == k:
        return {t: 1 / k for t in topics}
    return {
        t: (1 - params.epsilon) / c if t in chosen else params.epsilon / (k - c)
        for t in topics
    }


def _apply_delta(
    weights: dict[int, float],
    start: dict[int, float],
    event: Event,
    topic: int,
    delta: float,
    chosen: AbstractSet[int],
    params: Params,
) -> dict[int, float]:
    """One weight update of spec 4.2, followed by renormalisation."""
    w = dict(weights)
    if delta > 0:
        w[topic] = w[topic] + delta
    else:
        floor = 0.0 if (event.type == "hide" and topic not in chosen) else params.w_floor
        w[topic] = max(w[topic] + delta, min(w[topic], floor))
    total = sum(w.values())
    if total <= 0:
        return dict(start)
    return {t: v / total for t, v in w.items()}


def _is_strong_positive(event: Event) -> bool:
    """Events that can make a topic the echo topic (spec 4.4)."""
    if event.type in ECHO_TYPES:
        return True
    if event.type == "read":
        depth = _payload_number(event.payload, "scrollDepth")
        return depth is not None and depth > ECHO_READ_SCROLL_DEPTH
    return False


def build_profile(
    events: Sequence[Event],
    all_topics: Sequence[int],
    chosen: AbstractSet[int],
    now: datetime,
    last_request_at: datetime | None,
    params: Params,
) -> Profile:
    """Replay the reader's events into a profile (spec 4.2 to 4.4).

    Events are replayed oldest first, ordered by (occurred_at, event_id); an event_id
    seen twice is applied once. `last_request_at` None makes every event eligible to
    the echo. Truncation to EVENTS_MAX is the caller's job.
    """
    replay: list[Event] = []
    seen: set[str] = set()
    for event in sorted(events, key=lambda e: (e.occurred_at, e.event_id)):
        if event.event_id not in seen:
            seen.add(event.event_id)
            replay.append(event)

    start = initial_weights(all_topics, chosen, params)
    weights = dict(start)
    states: dict[str, ArticleState] = {}
    last_event_at: dict[str, datetime] = {}   # insertion order = order of last event
    history: list[WeightChange] = []
    echo_event: Event | None = None

    for event in replay:
        before = states.get(event.article_id, ArticleState())
        after = apply_event(before, event, params)
        states[event.article_id] = after
        last_event_at.pop(event.article_id, None)
        last_event_at[event.article_id] = event.occurred_at

        if (last_request_at is None or event.received_at > last_request_at) and _is_strong_positive(event):
            echo_event = event

        delta = article_value(after, params) - article_value(before, params)
        topic = event.topic_id
        if delta == 0 or topic not in weights:
            continue
        weights = _apply_delta(weights, start, event, topic, delta, chosen, params)
        history.append(WeightChange(
            event_id=event.event_id,
            type=event.type,
            article_id=event.article_id,
            topic_id=topic,
            delta=delta,
            weight_after=weights[topic],
            occurred_at=event.occurred_at,
        ))

    liked: list[ProfileItem] = []
    rejected: list[ProfileItem] = []
    for article_id in reversed(last_event_at):
        state = states[article_id]
        value = article_value(state, params)
        if value == 0:
            continue
        last = last_event_at[article_id]
        age_days = max((now - last) / timedelta(days=1), 0.0)
        decay = 0.5 ** (age_days / params.half_life_profile_days)
        if value > 0:
            if len(liked) < params.profile_max_items:
                strength = min(1.0, value / params.v_like)
                liked.append(ProfileItem(article_id, strength * decay, last))
        elif len(rejected) < params.profile_max_items:
            strength = 1.0 if state.hidden else params.s_skip
            rejected.append(ProfileItem(article_id, strength * decay, last))

    echo_topic = None
    if echo_event is not None and weights.get(echo_event.topic_id, 0.0) > 0:
        echo_topic = echo_event.topic_id

    return Profile(
        weights=weights,
        liked=liked,
        rejected=rejected,
        echo_topic=echo_topic,
        touched_article_ids=frozenset(states),
        history=history,
    )
