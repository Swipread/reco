"""Score of a candidate article: proximity, rejection, freshness (spec section 5)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from swipread_reco.params import Params

# Definitions fixed by spec 5.1 and 5.2 (not tunables of section 10).
MU_MIN_ROWS = 50        # below this catalog size, MU = 0
AFFINITY_TOP_K = 3      # proximity = mean of the top min(3, liked) weighted sims
SECONDS_PER_HOUR = 3600.0


def baseline_mu(embeddings: np.ndarray, n_pairs: int = 2000, seed: int = 0) -> float:
    """Mean cosine between random distinct pairs of rows (spec 5.1); 0.0 under MU_MIN_ROWS rows."""
    n = embeddings.shape[0]
    if n < MU_MIN_ROWS:
        return 0.0
    rng = np.random.default_rng(seed)
    i = rng.integers(0, n, n_pairs)
    j = (i + 1 + rng.integers(0, n - 1, n_pairs)) % n
    e = embeddings.astype(np.float64)
    cosines = np.einsum("ij,ij->i", e[i], e[j])
    return float(cosines.mean())


def sim(a: np.ndarray, b: np.ndarray, mu: float) -> np.ndarray:
    """Recentred similarity (n,d),(m,d) -> (n,m), clipped to [0, 1] (spec 5.1)."""
    cos = a.astype(np.float64) @ b.astype(np.float64).T
    return np.clip((cos - mu) / (1.0 - mu), 0.0, 1.0)


@dataclass(frozen=True)
class Scores:
    affinity: np.ndarray        # (n,)
    penalty: np.ndarray         # (n,)
    freshness: np.ndarray       # (n,)
    raw: np.ndarray             # (n,) unbounded, used for ranking
    score: np.ndarray           # (n,) clipped to [0, 1]
    liked_sim: np.ndarray       # (n, L) unweighted similarity
    rejected_sim: np.ndarray    # (n, R)


def score_candidates(
    embeddings: np.ndarray, published_at: Sequence[datetime],
    liked_emb: np.ndarray, liked_strength: np.ndarray,
    rejected_emb: np.ndarray, rejected_strength: np.ndarray,
    mu: float, now: datetime, params: Params,
) -> Scores:
    """Score every candidate row against the reader's liked and rejected articles (spec 5.2, 5.3).

    `liked_*` and `rejected_*` may have zero rows: the matching term is then 0.
    """
    n = embeddings.shape[0]

    liked_sim = sim(embeddings, liked_emb, mu)
    n_liked = liked_sim.shape[1]
    if n_liked == 0:
        affinity = np.zeros(n)
    else:
        weighted = liked_sim * np.asarray(liked_strength, dtype=np.float64)[None, :]
        k = min(AFFINITY_TOP_K, n_liked)
        affinity = np.sort(weighted, axis=1)[:, -k:].mean(axis=1)

    rejected_sim = sim(embeddings, rejected_emb, mu)
    if rejected_sim.shape[1] == 0:
        penalty = np.zeros(n)
    else:
        weighted = rejected_sim * np.asarray(rejected_strength, dtype=np.float64)[None, :]
        penalty = weighted.max(axis=1)

    published_s = np.fromiter((p.timestamp() for p in published_at), dtype=np.float64, count=n)
    age_hours = np.maximum((now.timestamp() - published_s) / SECONDS_PER_HOUR, 0.0)
    freshness = 0.5 ** (age_hours / params.half_life_fresh_hours)

    raw = params.w_aff * affinity + params.w_fresh * freshness - params.w_neg * penalty
    return Scores(
        affinity=affinity,
        penalty=penalty,
        freshness=freshness,
        raw=raw,
        score=np.clip(raw, 0.0, 1.0),
        liked_sim=liked_sim,
        rejected_sim=rejected_sim,
    )
