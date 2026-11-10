"""Purge of old recommendation logs (spec 8.7)."""

import logging
from datetime import datetime

from swipread_reco.store import Store

DEFAULT_RETENTION_DAYS = 30   # spec 8.7

log = logging.getLogger(__name__)


def run_purge(store: Store, now: datetime, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Delete lots older than `retention_days` or of deleted users; the number of lots deleted."""
    deleted = store.purge(now, retention_days)
    log.info("purged %d feed requests", deleted)
    return deleted
