"""Shared helpers for unit tests built on the synthetic catalog (imported explicitly)."""

import dataclasses
from collections.abc import Sequence
from datetime import UTC, datetime

import numpy as np

from swipread_reco.models import CatalogRow, make_catalog
from swipread_reco.params import Params
from swipread_reco.scoring import Scores, score_candidates
from swipread_reco.sim.catalog import SimCatalog

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)


def rows_of(sim_catalog: SimCatalog, ids: Sequence[str]) -> list[CatalogRow]:
    """Catalog rows of `ids`, rebuilt from the synthetic catalog."""
    c = sim_catalog.catalog
    rows = []
    for i in ids:
        k = c.index[i]
        rows.append(CatalogRow(
            id=i,
            topic_id=int(c.topic_ids[k]),
            source_id=int(c.source_ids[k]),
            language=c.languages[k],
            cluster_id=c.cluster_ids[k],
            published_at=c.published_at[k],
            embedding=c.embeddings[k],
        ))
    return rows


NEAR_DUPLICATE_NOISE = 0.1   # cosine ≈ 0.995 with the original: same story (≥ DUP_SIM)


def near_duplicate(row: CatalogRow, article_id: str, seed: int = 0) -> CatalogRow:
    """The same story as `row` from another outlet: slightly moved embedding, next source.

    The synthetic catalog has no such pairs (two articles of a subtopic sit near 0.65).
    """
    noise = np.random.default_rng(seed).standard_normal(row.embedding.shape[0])
    moved = row.embedding + NEAR_DUPLICATE_NOISE * noise / np.linalg.norm(noise)
    return dataclasses.replace(
        row, id=article_id, source_id=row.source_id + 1,
        embedding=(moved / np.linalg.norm(moved)).astype(np.float32),
    )


def _subtopic(row: CatalogRow) -> tuple[int, int]:
    """(topic, subtopic) read from a synthetic id `a_{topic}_{sub}_{i}`; subtopic -1 otherwise."""
    parts = row.id.split("_")
    if len(parts) == 4 and parts[0] == "a" and parts[2].isdigit():
        return row.topic_id, int(parts[2])
    return row.topic_id, -1


def sim_from(rows: Sequence[CatalogRow]) -> SimCatalog:
    """Synthetic catalog made of exactly `rows`, loaded at NOW."""
    return SimCatalog(catalog=make_catalog(rows, NOW), subtopic={r.id: _subtopic(r) for r in rows})


def scores_with(
    sim_catalog: SimCatalog, liked: Sequence[str] = (), rejected: Sequence[str] = ()
) -> Scores:
    """Scores of every catalog row against `liked` and `rejected` (strength 1), at NOW."""
    c = sim_catalog.catalog
    dim = c.embeddings.shape[1]

    def emb(ids: Sequence[str]) -> np.ndarray:
        if not ids:
            return np.zeros((0, dim), np.float32)
        return np.stack([c.embeddings[c.index[i]] for i in ids])

    return score_candidates(
        c.embeddings, c.published_at,
        emb(liked), np.ones(len(liked)),
        emb(rejected), np.ones(len(rejected)),
        c.mu, NOW, Params(),
    )
