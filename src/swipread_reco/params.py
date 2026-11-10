"""Tunable parameters of the recommender (spec section 10), overridable by environment."""

import dataclasses
import os
from collections.abc import Mapping
from dataclasses import dataclass

MODEL_VERSION = "topic-weights-v1"
EMBEDDING_MODEL = "multilingual-e5-small"

ENV_PREFIX = "RECO_"


@dataclass(frozen=True)
class Params:
    epsilon: float = 0.10
    w_floor: float = 0.01
    p_max: float = 0.60
    t_skip_ms: int = 1500
    t_long_ms: int = 6000
    v_skip: float = -0.02
    v_long_view: float = 0.02
    v_open: float = 0.03
    v_read_bonus: float = 0.04
    v_like: float = 0.08
    v_hide: float = -0.15
    s_skip: float = 0.15
    half_life_profile_days: float = 30.0
    profile_max_items: int = 100
    events_max: int = 5000
    w_aff: float = 0.6
    w_fresh: float = 0.4
    w_neg: float = 0.5
    half_life_fresh_hours: float = 48.0
    max_age_days: int = 14
    dup_sim: float = 0.80
    source_max: int = 2
    aff_reason_min: float = 0.20
    catalog_refresh_s: int = 60
    encode_interval_s: int = 15

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Params":
        """Build Params, overriding each field from `RECO_<FIELD_NAME_UPPER>` when set."""
        overrides: dict[str, float | int] = {}
        for field in dataclasses.fields(cls):
            name = ENV_PREFIX + field.name.upper()
            raw = env.get(name)
            if raw is None:
                continue
            try:
                overrides[field.name] = field.type(raw)
            except ValueError as exc:
                raise ValueError(
                    f"{name}={raw!r} is not a valid {field.type.__name__}"
                ) from exc
        return cls(**overrides)

    def as_dict(self) -> dict[str, float | int]:
        return dataclasses.asdict(self)
