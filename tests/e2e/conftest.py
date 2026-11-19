"""Fixtures of the end-to-end tests, run by scripts/e2e.sh against the real backend.

Skipped unless the environment the script exports is set (E2E_* below).
"""

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import redis
from pgvector.psycopg import register_vector

from .client import Backend

REQUIRED_ENV = (
    "E2E_BACKEND_URL", "E2E_RECO_URL", "E2E_ADMIN_DSN", "E2E_RECO_DSN", "E2E_REDIS_URL",
    "E2E_RUN_DIR",
)


def pytest_runtest_setup(item: pytest.Item) -> None:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if item.get_closest_marker("e2e") and missing:
        pytest.skip(f"run by scripts/e2e.sh ({', '.join(missing)} not set)")


@pytest.fixture(scope="session")
def backend() -> Iterator[Backend]:
    with httpx.Client(base_url=os.environ["E2E_BACKEND_URL"], timeout=10) as http:
        yield Backend(http)


@pytest.fixture(scope="session")
def reco() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=os.environ["E2E_RECO_URL"], timeout=10) as http:
        yield http


@pytest.fixture(scope="session")
def admin() -> Iterator[psycopg.Connection]:
    """Superuser autocommit connection to the shared database."""
    with psycopg.connect(os.environ["E2E_ADMIN_DSN"], autocommit=True) as conn:
        register_vector(conn)
        yield conn


@pytest.fixture(scope="session")
def rds() -> Iterator[redis.Redis]:
    client = redis.Redis.from_url(os.environ["E2E_REDIS_URL"], decode_responses=True)
    yield client
    client.close()


@pytest.fixture(scope="session")
def topic_ids(admin: psycopg.Connection) -> dict[str, int]:
    """slug -> id of the backend's topics."""
    return dict(admin.execute("SELECT slug, id FROM app.topics").fetchall())


@pytest.fixture(scope="session")
def measure() -> Callable[..., None]:
    """Append one JSON line of measured values to $E2E_RUN_DIR/measures.txt (printed by the script)."""
    path = Path(os.environ["E2E_RUN_DIR"]) / "measures.txt"

    def write(scenario: str, **values: Any) -> None:
        with path.open("a") as f:
            f.write(json.dumps({"scenario": scenario, **values}, default=str) + "\n")

    return write
