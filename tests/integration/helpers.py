"""Plain helpers for the PostgreSQL integration tests (imported explicitly)."""

from datetime import UTC, datetime

import numpy as np

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)
DIM = 384  # app.articles.embedding is vector(384) in docs/database.md


def unit_vector(seed: int) -> np.ndarray:
    """Deterministic float32 vector of norm 1 and dimension DIM."""
    v = np.random.default_rng(seed).standard_normal(DIM).astype(np.float32)
    return v / np.linalg.norm(v)
