"""In-memory catalog: reload, atomic swap, failures (spec 8.2)."""

import time
from datetime import timedelta

from swipread_reco.catalog_cache import CatalogCache
from swipread_reco.params import Params

from ..fakes import NOW


def test_get_is_none_before_the_first_refresh(store):
    assert CatalogCache(store, Params(), lambda: NOW).get() is None


def test_refresh_failure_keeps_previous_catalog(store):          # Review Focus 4
    cache = CatalogCache(store, Params(), lambda: NOW)
    assert cache.refresh() is True
    old = cache.get()
    store.fail = True
    assert cache.refresh() is False and cache.get() is old


def test_refresh_failure_without_previous_catalog_leaves_none(store):
    store.fail = True
    cache = CatalogCache(store, Params(), lambda: NOW)
    assert cache.refresh() is False and cache.get() is None


def test_refresh_swaps_atomically_and_recomputes_mu(store):
    cache = CatalogCache(store, Params(), lambda: NOW)
    cache.refresh()
    first = cache.get()
    store.drop_articles(store.sim.ids(1))
    cache.refresh()
    assert len(cache.get().ids) == 1620 and len(first.ids) == 1800
    assert cache.get().mu != first.mu


def test_refresh_keeps_only_articles_younger_than_max_age(store):
    cache = CatalogCache(store, Params(max_age_days=14), lambda: NOW)
    cache.refresh()
    assert len(cache.get().ids) == 1800
    late = CatalogCache(store, Params(max_age_days=14), lambda: NOW + timedelta(days=14))
    late.refresh()
    assert late.get().ids == []


def test_start_refreshes_in_the_background_until_stopped(store):
    cache = CatalogCache(store, Params(catalog_refresh_s=0), lambda: NOW)
    cache.start()
    try:
        deadline = time.monotonic() + 2.0
        while cache.get() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cache.get() is not None
    finally:
        cache.stop()
    after_stop = cache.get()
    time.sleep(0.05)
    assert cache.get() is after_stop
