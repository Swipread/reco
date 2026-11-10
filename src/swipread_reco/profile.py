"""Reader profile built from interaction events (spec section 4)."""

import dataclasses
import math
from dataclasses import dataclass
from typing import Any

from swipread_reco.models import Event
from swipread_reco.params import Params

# Definitions fixed by spec 4.1 (not tunables of section 10): expected reading time
# is word_count / READING_WPM minutes, or FALLBACK_READ_MS when word_count is 0 or unknown.
READING_WPM = 230
MS_PER_MINUTE = 60_000
FALLBACK_READ_MS = 60_000


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


def _duration_ms(payload: Any, key: str) -> float | None:
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
            dwell = _duration_ms(event.payload, "dwellMs")
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
            read_ms = _duration_ms(event.payload, "readMs")
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
