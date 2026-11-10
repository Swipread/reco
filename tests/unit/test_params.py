import pytest

from swipread_reco.params import Params


def test_defaults_match_spec_section_10():
    p = Params()
    assert (p.epsilon, p.w_floor, p.p_max) == (0.10, 0.01, 0.60)
    assert (p.v_skip, p.v_long_view, p.v_open, p.v_read_bonus, p.v_like, p.v_hide) == (
        -0.02, 0.02, 0.03, 0.04, 0.08, -0.15)
    assert (p.w_aff, p.w_fresh, p.w_neg) == (0.6, 0.4, 0.5)
    assert (p.dup_sim, p.source_max, p.aff_reason_min) == (0.80, 2, 0.20)


def test_from_env_overrides_and_keeps_types():
    p = Params.from_env({"RECO_P_MAX": "0.5", "RECO_SOURCE_MAX": "3"})
    assert p.p_max == 0.5 and p.source_max == 3 and isinstance(p.source_max, int)


def test_from_env_rejects_unparseable_value():
    with pytest.raises(ValueError, match="RECO_P_MAX"):
        Params.from_env({"RECO_P_MAX": "abc"})
