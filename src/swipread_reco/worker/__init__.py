"""The worker process: encoding (8.4), fallback lists (8.5) and purge (8.7) on their own periods."""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from redis import Redis

from swipread_reco.params import Params
from swipread_reco.store import Store
from swipread_reco.worker.encode import Encoder, encode_pending
from swipread_reco.worker.fallback import write_fallback
from swipread_reco.worker.purge import run_purge

FALLBACK_INTERVAL_S = 600    # spec 8.5: every 10 minutes
PURGE_INTERVAL_S = 3600      # spec 8.7: every hour

log = logging.getLogger(__name__)


def run_worker(
    store: Store, redis: Redis, encoder: Encoder, params: Params,
    clock: Callable[[], datetime], sleep: Callable[[float], None],
) -> None:
    """Run the three activities forever, each at its own period, all of them at start.

    An exception in an activity is logged and the activity is retried at its next period;
    only an exception raised by `sleep` (or a KeyboardInterrupt) ends the loop.
    """
    activities: list[tuple[str, int, Callable[[datetime], object]]] = [
        ("encode", params.encode_interval_s, lambda now: encode_pending(store, encoder, now)),
        ("fallback", FALLBACK_INTERVAL_S, lambda now: write_fallback(store, redis, now, params)),
        ("purge", PURGE_INTERVAL_S, lambda now: run_purge(store, now)),
    ]
    start = clock()
    due = {name: start for name, _, _ in activities}
    while True:
        for name, period_s, run in activities:
            now = clock()
            if now < due[name]:
                continue
            try:
                run(now)
            except Exception:
                log.exception("worker %s failed", name)
            period = timedelta(seconds=period_s)
            # On schedule; after an overrun, one period from now rather than a burst of catch-ups.
            due[name] = due[name] + period if due[name] + period > now else now + period
        sleep(max(0.0, (min(due.values()) - clock()).total_seconds()))
