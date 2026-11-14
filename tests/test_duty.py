import pytest

from tariff_agent import duty
from tariff_agent.collector import fmt_hts, parse_ad_valorem
from tariff_agent.duty import Duty


def test_landed_cost_and_annual_cost():
    assert duty.duty_per_unit(100.0, 25.0) == pytest.approx(25.0)
    assert duty.landed_cost_per_unit(100.0, 25.0) == pytest.approx(125.0)
    assert duty.annual_cost(2.0, 1000, 10.0) == pytest.approx(2200.0)
    assert duty.landed_cost_per_unit(50.0, 0.0) == 50.0


def test_no_threats_all_scenarios_equal_base():
    s = duty.scenario_rates(3.5, [])
    assert s.today_pct == s.likely_pct == s.worst_pct == 3.5


def test_in_force_duty_counts_today_and_range_top_in_worst():
    s = duty.scenario_rates(2.0, [Duty("232", 25, 50, 1.0, True)])
    assert s.today_pct == pytest.approx(27.0)      # base + bottom of in-force range
    assert s.likely_pct == pytest.approx(27.0)     # nothing pending
    assert s.worst_pct == pytest.approx(52.0)      # top of range


def test_pending_duty_is_probability_weighted_on_midpoint():
    s = duty.scenario_rates(0.0, [Duty("232", 20, 40, 0.5, False)])
    assert s.today_pct == 0.0
    assert s.likely_pct == pytest.approx(15.0)     # 0.5 * midpoint 30
    assert s.worst_pct == pytest.approx(40.0)


def test_same_authority_does_not_stack_and_only_increment_counts():
    duties = [Duty("232", 25, 25, 1.0, True), Duty("232", 50, 50, 0.4, False)]
    s = duty.scenario_rates(0.0, duties)
    assert s.today_pct == pytest.approx(25.0)
    assert s.likely_pct == pytest.approx(25.0 + 0.4 * 25.0)   # expected increase over the 25 already paid
    assert s.worst_pct == pytest.approx(50.0)                 # max, not 75


def test_pending_duty_below_current_rate_adds_nothing():
    s = duty.scenario_rates(0.0, [Duty("232", 50, 50, 1.0, True), Duty("232", 10, 20, 0.9, False)])
    assert s.likely_pct == pytest.approx(50.0)
    assert s.worst_pct == pytest.approx(50.0)


def test_different_authorities_add_up():
    s = duty.scenario_rates(5.0, [Duty("301", 10, 10, 1.0, True), Duty("232", 25, 25, 1.0, True)])
    assert s.today_pct == pytest.approx(40.0)


def test_section_122_does_not_stack_on_section_232():
    assert duty.combine_by_authority({"232": 25.0, "122": 10.0}) == pytest.approx(25.0)
    assert duty.combine_by_authority({"301": 25.0, "122": 10.0}) == pytest.approx(35.0)
    assert duty.combine_by_authority({"232": 0.0, "122": 10.0}) == pytest.approx(10.0)


def test_scenarios_are_ordered():
    duties = [Duty("301", 10, 12.5, 1.0, True), Duty("301", 10, 25, 0.2, False), Duty("232", 25, 50, 0.1, False)]
    s = duty.scenario_rates(1.5, duties)
    assert s.base_pct <= s.today_pct <= s.likely_pct <= s.worst_pct


def test_cost_table_dollars():
    s = duty.scenario_rates(0.0, [Duty("232", 20, 40, 0.5, False)])
    t = duty.cost_table(unit_cost=10.0, annual_units=1000, s=s)
    assert t["today_per_unit"] == pytest.approx(10.0)
    assert t["likely_per_unit"] == pytest.approx(11.5)
    assert t["worst_per_unit"] == pytest.approx(14.0)
    assert t["likely_delta_per_year"] == pytest.approx(1500.0)
    assert t["worst_delta_per_year"] == pytest.approx(4000.0)


def test_invalid_inputs_rejected():
    with pytest.raises(ValueError):
        Duty("232", 30, 20, 0.5, False)
    with pytest.raises(ValueError):
        Duty("232", 10, 20, 1.5, False)
    with pytest.raises(ValueError):
        duty.scenario_rates(-1.0, [])


@pytest.mark.parametrize("raw,expected", [("Free", 0.0), ("3.5%", 3.5), (" 16.5% ", 16.5), ("5¢/kg on contents", None), (None, None), ("", None)])
def test_parse_ad_valorem(raw, expected):
    assert parse_ad_valorem(raw) == expected


def test_fmt_hts():
    assert fmt_hts("7318152030", 8) == "7318.15.20"
    assert fmt_hts("7318.15.20.30", 10) == "7318.15.20.30"
