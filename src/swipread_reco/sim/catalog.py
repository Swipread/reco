"""Synthetic article catalog for tests and simulated readers (spec 11.1)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from swipread_reco.models import Catalog, CatalogRow, make_catalog

# Geometry fixed by spec 11.1.
SUBTOPIC_NOISE = 0.6    # subtopic centre = normalise(topic centre + 0.6 x noise)
ARTICLE_NOISE = 0.7     # article = normalise(subtopic centre + 0.7 x noise)


@dataclass(frozen=True)
class SimCatalog:
    catalog: Catalog
    subtopic: dict[str, tuple[int, int]]     # article_id -> (topic_id, subtopic number 0..n-1)

    def ids(self, topic: int, sub: int | None = None) -> list[str]:
        """Article ids of a topic, or of one of its subtopics, in catalog order."""
        return [
            i for i in self.catalog.ids
            if self.subtopic[i][0] == topic and (sub is None or self.subtopic[i][1] == sub)
        ]

    def embeddings(self, ids: Sequence[str]) -> dict[str, np.ndarray]:
        return {i: self.catalog.embeddings[self.catalog.index[i]] for i in ids}


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v)


def _noise(rng: np.random.Generator, dim: int) -> np.ndarray:
    """Gaussian vector rescaled to norm 1."""
    return _unit(rng.standard_normal(dim))


def make_sim_catalog(
    now: datetime, seed: int = 0, n_topics: int = 10, n_subtopics: int = 3,
    per_subtopic: int = 60, n_sources: int = 8, dim: int = 384,
    age_hours: float | Sequence[float] = 24.0, language: str = "fr",
) -> SimCatalog:
    """Topics 1..n_topics, sources 1..n_sources round-robin, ages cycled in creation order."""
    rng = np.random.default_rng(seed)
    ages = [float(age_hours)] if isinstance(age_hours, int | float) else [float(a) for a in age_hours]
    rows: list[CatalogRow] = []
    subtopic: dict[str, tuple[int, int]] = {}
    for topic in range(1, n_topics + 1):
        topic_centre = _noise(rng, dim)
        for sub in range(n_subtopics):
            sub_centre = _unit(topic_centre + SUBTOPIC_NOISE * _noise(rng, dim))
            for i in range(per_subtopic):
                k = len(rows)
                article_id = f"a_{topic}_{sub}_{i:02d}"
                rows.append(CatalogRow(
                    id=article_id,
                    topic_id=topic,
                    source_id=k % n_sources + 1,
                    language=language,
                    cluster_id=None,
                    published_at=now - timedelta(hours=ages[k % len(ages)]),
                    embedding=_unit(sub_centre + ARTICLE_NOISE * _noise(rng, dim)).astype(np.float32),
                ))
                subtopic[article_id] = (topic, sub)
    return SimCatalog(catalog=make_catalog(rows, now), subtopic=subtopic)
