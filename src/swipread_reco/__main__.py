"""Entry point: `python -m swipread_reco api` or `python -m swipread_reco worker`.

Both read RECO_DATABASE_URL, RECO_REDIS_URL, RECO_DB_TIMEOUT_S (default 2) and the RECO_*
overrides of Params; the api also reads RECO_HOST (default 127.0.0.1) and RECO_PORT
(default 8081, the backend's default swipread.reco.base-url), the worker RECO_STREAM_CONSUMER
(default `worker`, its name in the consumer group `reco` of the `events` stream).
"""

import argparse
import logging
import os
import signal
import time
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version

import redis
import uvicorn

from swipread_reco.api import RedisSeen, create_app, utcnow
from swipread_reco.catalog_cache import CatalogCache
from swipread_reco.params import Params
from swipread_reco.store import DEFAULT_POOL_TIMEOUT_S, PgStore
from swipread_reco.worker import run_worker
from swipread_reco.worker.encode import E5Encoder
from swipread_reco.worker.stream import DEFAULT_CONSUMER, StreamConsumer

# The routes are unauthenticated and reachable from the internal network only (spec 9.2):
# listen on loopback unless RECO_HOST says otherwise (e.g. 0.0.0.0 inside a container).
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8081  # the backend calls http://localhost:8081 by default (application.yaml)
# The worker is not on a request path: give Redis longer than the api's 100 ms.
WORKER_REDIS_TIMEOUT_S = 5.0

log = logging.getLogger("swipread_reco")


def package_version() -> str:
    """Version of the installed package (pyproject.toml, the release tag), or "unknown"."""
    try:
        return version("swipread-reco")
    except PackageNotFoundError:
        return "unknown"


def bind_address(env: Mapping[str, str] = os.environ) -> tuple[str, int]:
    """(host, port) uvicorn listens on, from RECO_HOST and RECO_PORT."""
    return env.get("RECO_HOST", DEFAULT_HOST), int(env.get("RECO_PORT", DEFAULT_PORT))


def open_store() -> PgStore:
    return PgStore(
        os.environ["RECO_DATABASE_URL"],
        timeout_s=float(os.environ.get("RECO_DB_TIMEOUT_S", DEFAULT_POOL_TIMEOUT_S)),
    )


def run_api() -> None:
    params = Params.from_env()
    store = open_store()
    seen = RedisSeen.from_url(os.environ["RECO_REDIS_URL"])
    cache = CatalogCache(store, params, utcnow)
    try:
        store.ensure_schema()
        cache.refresh()
        catalog = cache.get()
        log.info("catalog loaded: %d articles", len(catalog.ids) if catalog else 0)
        cache.start()
        app = create_app(store, seen, cache, params)
        host, port = bind_address()
        uvicorn.run(app, host=host, port=port)
    finally:
        cache.stop()
        store.close()


def exit_on_sigterm() -> None:
    """Turn SIGTERM (docker stop) into SystemExit, so that the worker closes its connections.

    As PID 1 in a container, a process without a handler ignores SIGTERM and is killed after
    the grace period. Work cut short is safe: an article not yet encoded is taken at the next
    start, a stream message not yet acknowledged is read again.
    """
    def stop(signum: int, frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)


def run_worker_process() -> None:
    exit_on_sigterm()
    params = Params.from_env()
    store = open_store()
    client = redis.Redis.from_url(
        os.environ["RECO_REDIS_URL"], decode_responses=True,
        socket_timeout=WORKER_REDIS_TIMEOUT_S, socket_connect_timeout=WORKER_REDIS_TIMEOUT_S,
    )
    try:
        store.ensure_schema()  # encode and purge need the reco tables even if the api never ran
        stream = StreamConsumer(
            client, store, params, utcnow, os.environ.get("RECO_STREAM_CONSUMER", DEFAULT_CONSUMER)
        )
        run_worker(store, client, E5Encoder(), params, utcnow, time.sleep, stream)
    finally:
        client.close()
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m swipread_reco")
    parser.add_argument("command", choices=["api", "worker"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.info("swipread-reco %s, %s", package_version(), args.command)
    if args.command == "api":
        run_api()
    else:
        run_worker_process()


if __name__ == "__main__":
    main()
