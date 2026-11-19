"""Consumer of the backend's `events` stream (contract v1.5, spec 8.6).

Reads the stream in the consumer group `reco` and, per batch, recomputes once the profile of
each reader that has a new profile event, from app.events (the source of truth), or deletes
everything kept on a reader on `user_deleted`. Recomputing from app.events makes the handling
idempotent and insensitive to the order of the messages.

A profile that fails to refresh is acknowledged anyway: the next request finds it stale and
recomputes it (profiles.current_profile). A `user_deleted` that fails is left pending and
retried by retry_pending(); the hourly purge deletes the same data as a safety net.
"""

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from redis import Redis
from redis.exceptions import RedisError, ResponseError

from swipread_reco.params import Params
from swipread_reco.profiles import refresh_profile
from swipread_reco.store import PROFILE_EVENT_TYPES, Store

STREAM = "events"
GROUP = "reco"
DEFAULT_CONSUMER = "worker"
BATCH = 100
USER_DELETED = "user_deleted"

log = logging.getLogger(__name__)

Message = tuple[str, Mapping[str, str]]


def parse_message(fields: Mapping[str, str]) -> dict[str, Any] | None:
    """The JSON of the message's single field `data`; None if missing or malformed."""
    try:
        message = json.loads(fields["data"])
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(message, dict) or not isinstance(message.get("userId"), str):
        return None
    if not isinstance(message.get("type"), str):
        return None
    return message


class StreamConsumer:
    """One consumer of the group `reco`. The worker runs a single one; its name, stable across
    restarts, lets it take back the messages it had read but not acknowledged."""

    def __init__(
        self, redis: Redis, store: Store, params: Params, clock: Callable[[], datetime],
        name: str = DEFAULT_CONSUMER,
    ) -> None:
        self._redis = redis
        self._store = store
        self._params = params
        self._clock = clock
        self._name = name
        self._group_ready = False

    def _ensure_group(self) -> None:
        """Create the group at the start of the stream (MKSTREAM) unless it exists: the first
        run reads the messages still kept (7 days) and builds those readers' profiles."""
        if self._group_ready:
            return
        try:
            self._redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
            log.info("created consumer group %s on stream %s", GROUP, STREAM)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True

    def _read(self, start: str, block_ms: int | None) -> list[Message]:
        self._ensure_group()
        try:
            response = self._redis.xreadgroup(
                GROUP, self._name, {STREAM: start}, count=BATCH, block=block_ms
            )
        except ResponseError:
            # NOGROUP: stream or group deleted (Redis flushed). Recreate it next time; if it
            # still exists, the creation only answers BUSYGROUP.
            self._group_ready = False
            raise
        return [(msg_id, fields) for _, messages in response or [] for msg_id, fields in messages]

    def poll(self, timeout_s: float) -> int:
        """Wait up to `timeout_s` for new messages and handle one batch; the number handled.

        Redis errors are logged and the wait is spent doing nothing more.
        """
        try:
            messages = self._read(">", max(1, int(timeout_s * 1000)))
        except RedisError:
            log.warning("events stream unreadable", exc_info=True)
            return 0
        return self.handle(messages)

    def retry_pending(self) -> int:
        """Handle again the messages read but not acknowledged (failed deletions, a crash)."""
        try:
            messages = self._read("0", None)
        except RedisError:
            log.warning("events stream unreadable", exc_info=True)
            return 0
        return self.handle(messages)

    def handle(self, messages: Sequence[Message]) -> int:
        """Handle one batch and acknowledge it, except the deletions that failed."""
        if not messages:
            return 0
        deleted: dict[str, list[str]] = {}    # user -> ids of their user_deleted messages
        to_refresh: dict[str, None] = {}      # users with a profile event, in arrival order
        done: list[str] = []
        for msg_id, fields in messages:
            message = parse_message(fields)
            if message is None:
                log.warning("events stream: malformed message %s, skipped", msg_id)
                done.append(msg_id)
            elif message["type"] == USER_DELETED:
                deleted.setdefault(message["userId"], []).append(msg_id)
            else:
                if message["type"] in PROFILE_EVENT_TYPES:
                    to_refresh.setdefault(message["userId"], None)
                done.append(msg_id)

        for user_id, ids in deleted.items():
            to_refresh.pop(user_id, None)
            try:
                self._store.delete_user_data(user_id)
                done.extend(ids)
            except Exception:
                log.exception("events stream: deleting the data of %s failed, will retry", user_id)

        topics: list[int] | None = None
        for user_id in to_refresh:
            try:
                if topics is None:
                    topics = [t.id for t in self._store.topics()]
                self._refresh(user_id, topics)
            except Exception:
                log.exception("events stream: profile of %s not refreshed", user_id)

        if done:
            try:
                self._redis.xack(STREAM, GROUP, *done)
            except RedisError:
                log.warning("events stream: acknowledgement failed", exc_info=True)
        return len(messages)

    def _refresh(self, user_id: str, topics: list[int]) -> None:
        if self._store.user_locale(user_id) is None:
            # Gone from app.users (its user_deleted may still be on its way): keep nothing.
            self._store.delete_user_data(user_id)
            return
        chosen = self._store.chosen_topics(user_id)
        refresh_profile(self._store, user_id, topics, chosen, self._params, self._clock())
