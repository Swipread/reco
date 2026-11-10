"""The api process's in-memory catalog, reloaded in the background (spec 8.2)."""

import logging
import threading
from collections.abc import Callable
from datetime import datetime

from swipread_reco.models import Catalog, make_catalog
from swipread_reco.params import Params
from swipread_reco.store import Store

log = logging.getLogger(__name__)


class CatalogCache:
    """Holds the current Catalog; each refresh builds a new one and swaps it in whole."""

    def __init__(self, store: Store, params: Params, clock: Callable[[], datetime]) -> None:
        self._store = store
        self._params = params
        self._clock = clock
        self._catalog: Catalog | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def refresh(self) -> bool:
        """Reload the catalog; on failure keep the previous one and return False."""
        now = self._clock()
        try:
            catalog = make_catalog(self._store.catalog_rows(now, self._params.max_age_days), now)
        except Exception:
            log.exception("catalog refresh failed, keeping the previous catalog")
            return False
        self._catalog = catalog  # a single assignment: readers see the old or the new one
        return True

    def get(self) -> Catalog | None:
        """The last catalog loaded, None if no refresh has succeeded yet."""
        return self._catalog

    def start(self) -> None:
        """Refresh every catalog_refresh_s seconds in a daemon thread, until stop()."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="catalog-refresh", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join()
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._params.catalog_refresh_s):
            self.refresh()
