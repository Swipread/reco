"""Entry point: `python -m swipread_reco api`.

Reads RECO_DATABASE_URL, RECO_REDIS_URL, RECO_HOST (default 127.0.0.1), RECO_PORT
(default 8000), RECO_DB_TIMEOUT_S (default 2) and the RECO_* overrides of Params.
"""

import argparse
import logging
import os
from collections.abc import Mapping

import uvicorn

from swipread_reco.api import RedisSeen, create_app, utcnow
from swipread_reco.catalog_cache import CatalogCache
from swipread_reco.params import Params
from swipread_reco.store import DEFAULT_POOL_TIMEOUT_S, PgStore

# The routes are unauthenticated and reachable from the internal network only (spec 9.2):
# listen on loopback unless RECO_HOST says otherwise (e.g. 0.0.0.0 inside a container).
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

log = logging.getLogger("swipread_reco")


def bind_address(env: Mapping[str, str] = os.environ) -> tuple[str, int]:
    """(host, port) uvicorn listens on, from RECO_HOST and RECO_PORT."""
    return env.get("RECO_HOST", DEFAULT_HOST), int(env.get("RECO_PORT", DEFAULT_PORT))


def run_api() -> None:
    params = Params.from_env()
    store = PgStore(
        os.environ["RECO_DATABASE_URL"],
        timeout_s=float(os.environ.get("RECO_DB_TIMEOUT_S", DEFAULT_POOL_TIMEOUT_S)),
    )
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m swipread_reco")
    parser.add_argument("command", choices=["api"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command == "api":
        run_api()


if __name__ == "__main__":
    main()
