"""Per-topic fallback lists in Redis (spec 8.5)."""

import logging
from datetime import datetime

from redis import Redis

from swipread_reco.params import Params
from swipread_reco.store import Store

DEFAULT_LIMIT = 50       # articles per list (spec 8.5)
DEFAULT_TTL_S = 3600     # lifetime of a list (spec 8.5)

log = logging.getLogger(__name__)


def write_fallback(
    store: Store, redis: Redis, now: datetime, params: Params,
    limit: int = DEFAULT_LIMIT, ttl_s: int = DEFAULT_TTL_S,
) -> None:
    """For each topic, replace `fallback:{slug}` atomically by its `limit` newest articles.

    The list is built under `fallback:{slug}:tmp`, then RENAMEd and given its TTL in the same
    MULTI; a topic without articles has its list deleted.
    """
    for topic in store.topics():
        key = f"fallback:{topic.slug}"
        ids = store.fallback_ids(topic.id, now, params.max_age_days, limit)
        if not ids:
            redis.delete(key)
            continue
        tmp = f"{key}:tmp"
        with redis.pipeline(transaction=True) as pipe:
            pipe.delete(tmp)
            pipe.rpush(tmp, *ids)
            pipe.rename(tmp, key)
            pipe.expire(key, ttl_s)
            pipe.execute()
    log.info("fallback lists written")
