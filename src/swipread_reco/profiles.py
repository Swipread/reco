"""The reader's profile as kept in reco.user_profiles (contract v1.5): fresh or recomputed.

`app.events` stays the source of truth. A stored profile is used only if it was computed from
the same events (its mark), the same topics and chosen topics, the same parameters and model;
otherwise it is replayed from `app.events` and stored again. The worker keeps profiles fresh
from the `events` stream, so a request usually finds its profile ready; a request that comes
before its message, or after a lost one, recomputes it itself.
"""

import logging
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from datetime import datetime
from typing import Any

from swipread_reco.models import ProfileState, StoredProfile
from swipread_reco.params import MODEL_VERSION, Params
from swipread_reco.profile import replay_profile
from swipread_reco.store import Store

log = logging.getLogger(__name__)


def profile_basis(
    all_topics: Sequence[int], chosen: AbstractSet[int], params: Params
) -> dict[str, Any]:
    """What a replay depends on besides the events: a stored profile is stale when it changes."""
    return {
        "modelVersion": MODEL_VERSION,
        "params": params.as_dict(),
        "topics": list(all_topics),
        "chosen": sorted(chosen),
    }


def refresh_profile(
    store: Store, user_id: str, all_topics: Sequence[int], chosen: AbstractSet[int],
    params: Params, now: datetime,
) -> ProfileState:
    """Replay the reader's events from app.events and store the result with its mark."""
    mark, events = store.profile_inputs(user_id, params.events_max)
    state = replay_profile(events, all_topics, chosen, params)
    store.save_profile(
        user_id, StoredProfile(profile_basis(all_topics, chosen, params), mark, state), now
    )
    return state


def current_profile(
    store: Store, user_id: str, all_topics: Sequence[int], chosen: AbstractSet[int],
    params: Params, now: datetime,
) -> ProfileState:
    """The stored profile if it is fresh, else a replay of app.events (stored in turn)."""
    stored = store.stored_profile(user_id)
    if (
        stored is not None
        and stored.basis == profile_basis(all_topics, chosen, params)
        and stored.mark == store.event_mark(user_id)
    ):
        return stored.state
    return refresh_profile(store, user_id, all_topics, chosen, params, now)
