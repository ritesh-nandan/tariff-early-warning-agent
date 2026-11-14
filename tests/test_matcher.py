from datetime import date

import pytest

from tariff_agent import matcher, scoring
from tariff_agent.matcher import Product
from tariff_agent.schemas import KeyDates, ThreatCard
from tariff_agent.scoring import Threat


def card(**kw) -> ThreatCard:
    base = dict(authority="232", status="comments open", likelihood_90d=0.1, rationale="r", source_url="https://example.gov/x")
    base.update(kw)
    return ThreatCard(**base)


def threat(title="Notice", pub="2026-01-01", doc_id="D1", topics=None, **kw) -> Threat:
    c = card(**kw)
    from tariff_agent import concordance
    return Threat(doc_id, title, pub, "https://example.gov/" + doc_id, c, "heuristic",
                  topics if topics is not None else concordance.topics_for_notice(title, c.affected_categories))


BOLT = Product("STL", "Structural steel bolts", "7318152030", "China", 0.42, 1000)
DRONE = Product("UAS", "Camera drone", "8806220000", "China", 400, 10)
COFFEE = Product("COF", "Green coffee beans", "0901110025", "Colombia", 4.1, 10)


def test_hts_prefix_match_strength_by_specificity():
    assert matcher.match_product(BOLT, threat(hts_codes=["73"])).strength == 0.6
    assert matcher.match_product(BOLT, threat(hts_codes=["7318"])).strength == 0.8
    m = matcher.match_product(BOLT, threat(hts_codes=["7318.15", "7208"]))
    assert m.method == "hts" and m.strength == 1.0 and "7318.15" in m.reason


def test_no_match_when_hts_and_topic_differ():
    assert matcher.match_product(COFFEE, threat(title="Adjusting Imports of Copper", hts_codes=["7408"])) is None


def test_country_filter():
    t = threat(hts_codes=["7318"], countries=["Brazil"])
    assert matcher.match_product(BOLT, t) is None
    assert matcher.match_product(BOLT, threat(hts_codes=["7318"], countries=["People's Republic of China"])) is not None
    assert matcher.match_product(BOLT, threat(hts_codes=["7318"], countries=["ALL"])) is not None
    assert matcher.match_product(BOLT, threat(hts_codes=["7318"], countries=[])) is not None  # not stated


def test_country_wide_measure_matches_any_product_from_that_country():
    t = threat(title="Section 301 action", authority="301", countries=["China"], applies_to_all_products=True)
    m = matcher.match_product(DRONE, t)
    assert m.method == "country-wide" and m.strength == 0.9
    assert matcher.match_product(COFFEE, t) is None


def test_concordance_match_when_notice_lists_no_codes():
    t = threat(title="Section 232 National Security Investigation of Imports of Unmanned Aircraft Systems")
    m = matcher.match_product(DRONE, t)
    assert m.method == "concordance" and m.strength == 0.7
    assert matcher.match_product(COFFEE, t) is None


def test_derivative_match_is_weak_and_flagged_unverified_offline():
    furniture = Product("F", "Powder-coated steel shelving unit", "9403200086", "Vietnam", 30, 10)
    t = threat(title="Adjusting Imports of Steel Into the United States")
    m = matcher.match_product(furniture, t)
    assert m.method == "derivative" and m.strength < 0.5
    matched = matcher.match_all(furniture, [t])
    assert "Unverified" in matched[0][1].reason


def test_crude_oil_api_gravity_is_not_a_pharmaceutical():
    oil = Product("OIL", "Crude petroleum oil 25 degrees API or more", "2709002090", "Brazil", 70, 10)
    t = threat(title="Adjusting Imports of Pharmaceuticals and Pharmaceutical Ingredients Into the United States")
    assert matcher.match_product(oil, t) is None


def test_load_products_valid_and_errors():
    text = "sku,description,hts_code,country_of_origin,unit_cost_usd,annual_units\nA,Thing,7318.15.20.30,China,$1.50,\"1,000\"\n"
    p = matcher.load_products(text)[0]
    assert (p.hts_code, p.unit_cost_usd, p.annual_units) == ("7318152030", 1.5, 1000.0)
    with pytest.raises(ValueError, match="missing column"):
        matcher.load_products("sku,description\nA,B\n")
    with pytest.raises(ValueError, match="at least 6 digits"):
        matcher.load_products("sku,description,hts_code,country_of_origin,unit_cost_usd,annual_units\nA,B,73,China,1,1\n")
    with pytest.raises(ValueError, match="must be numbers"):
        matcher.load_products("sku,description,hts_code,country_of_origin,unit_cost_usd,annual_units\nA,B,731815,China,x,1\n")


def test_schema_drops_chapter_99_codes_and_validates_dates():
    c = card(hts_codes=["9903.81.90", "7318.15", "7318.15"])
    assert c.hts_codes == ["731815"]
    with pytest.raises(ValueError):
        card(key_dates={"effective_date": "August 1, 2025"})
    with pytest.raises(ValueError):
        card(rate_low_pct=50, rate_high_pct=25)


def test_status_and_likelihood_over_time():
    t = threat(status="action announced", rate_low_pct=25, rate_high_pct=25, pub="2026-08-11",
               key_dates=KeyDates(effective_date="2026-12-04"))
    assert scoring.effective_status(t, date(2026, 11, 14)) == "action announced"
    assert scoring.likelihood_now(t, date(2026, 11, 14)) == 0.95
    assert scoring.effective_status(t, date(2026, 12, 5)) == "in force"
    assert scoring.likelihood_now(t, date(2026, 12, 5)) == 1.0


def test_section_122_surcharge_expires():
    t = threat(title="Imposing a Temporary Import Surcharge", authority="122", status="in force", pub="2026-02-25",
               countries=["ALL"], applies_to_all_products=True, rate_low_pct=10, rate_high_pct=10,
               key_dates=KeyDates(effective_date="2026-02-24", statutory_deadline="2026-07-24"))
    assert scoring.effective_status(t, date(2026, 6, 1)) == "in force"
    assert scoring.effective_status(t, date(2026, 8, 1)) == "expired"
    assert scoring.likelihood_now(t, date(2026, 8, 1)) == 0.0


def test_investigation_likelihood_ages_with_hazard_table():
    t = threat(title="Request for Public Comments on Section 232 National Security Investigation of Imports of Copper",
               pub="2025-03-13", likelihood_90d=0.05, key_dates=KeyDates(initiated="2025-03-10"))
    assert scoring.likelihood_now(t, date(2025, 3, 20)) == 0.05          # fresh: notice's own estimate
    assert scoring.likelihood_now(t, date(2025, 7, 1)) == 0.20           # 113 days in
    assert scoring.likelihood_now(t, date(2026, 3, 20)) == 0.75          # past 360 days


def test_later_notice_supersedes_earlier_one_on_same_topic():
    old = threat(title="Investigation of Imports of Copper", pub="2025-03-13", doc_id="A")
    new = threat(title="Adjusting Imports of Copper Into the United States", pub="2025-08-05", doc_id="B",
                 status="in force", rate_low_pct=50, rate_high_pct=50, key_dates=KeyDates(effective_date="2025-08-01"))
    assert [t.doc_id for t in scoring.current_threats([old, new], date(2025, 7, 1))] == ["A"]
    assert [t.doc_id for t in scoring.current_threats([old, new], date(2025, 9, 1))] == ["B"]
