"""Fixtures for the HTTP API tests: in-memory store and seen sets, fixed clock NOW."""

import pytest
from fastapi.testclient import TestClient

from swipread_reco.api import create_app
from swipread_reco.catalog_cache import CatalogCache
from swipread_reco.params import Params

from ..fakes import NOW, FakeSeen, FakeStore


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def seen() -> FakeSeen:
    return FakeSeen()


@pytest.fixture
def cache(store: FakeStore) -> CatalogCache:
    """Catalog cache already loaded from `store` at NOW."""
    c = CatalogCache(store, Params(), lambda: NOW)
    assert c.refresh()
    return c


@pytest.fixture
def client(store: FakeStore, seen: FakeSeen, cache: CatalogCache) -> TestClient:
    return TestClient(create_app(store, seen, cache, Params(), clock=lambda: NOW))


@pytest.fixture
def empty_client(store: FakeStore, seen: FakeSeen) -> TestClient:
    """Same store and seen sets, but a cache that never loaded a catalog."""
    never_loaded = CatalogCache(store, Params(), lambda: NOW)
    return TestClient(create_app(store, seen, never_loaded, Params(), clock=lambda: NOW))
