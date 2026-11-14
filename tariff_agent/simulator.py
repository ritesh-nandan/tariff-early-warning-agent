"""Steps 4-5 glue: products x active threats -> exposure and landed-cost scenarios."""
from __future__ import annotations

from datetime import date

from . import collector, config, duty, matcher, scoring
from .matcher import Product
from .scoring import Threat

MIN_STRENGTH_FOR_TODAY = 0.6  # weaker matches never count as duties already being paid


def active_threats(threats: list[Threat], as_of: date) -> list[Threat]:
    """Current notices that still matter: not struck down and not lapsed."""
    return [t for t in scoring.current_threats(threats, as_of)
            if scoring.effective_status(t, as_of) not in ("struck down", "expired")]


def threat_summary(t: Threat, as_of: date) -> dict:
    c = t.card
    return {
        "doc_id": t.doc_id, "title": t.title, "url": t.url, "publication_date": t.publication_date,
        "authority": c.authority, "status": scoring.effective_status(t, as_of), "status_at_notice": c.status,
        "likelihood_now": round(scoring.likelihood_now(t, as_of), 2), "likelihood_at_notice": c.likelihood_90d,
        "categories": c.affected_categories, "hts_codes": c.hts_codes, "countries": c.countries,
        "applies_to_all_products": c.applies_to_all_products,
        "rate_low_pct": c.rate_low_pct, "rate_high_pct": c.rate_high_pct,
        "key_dates": c.key_dates.model_dump(), "other_dates": [o.model_dump() for o in c.other_dates],
        "rationale": c.rationale, "extractor": t.extractor, "topics": t.topics, "procedural": t.procedural,
    }


def _duty_for(t: Threat, m: matcher.Match, as_of: date) -> tuple[duty.Duty, dict]:
    c = t.card
    status = scoring.effective_status(t, as_of)
    low, high = c.rate_low_pct, c.rate_high_pct
    if low is None and high is None:
        if status == "in force":
            low, high, basis = 0.0, 0.0, "rate not stated in notice (counted as 0)"
        else:
            low, high = config.ASSUMED_RATE_RANGE_PCT[c.authority]
            basis = "assumed scenario range (notice states no rate)"
    else:
        low = low if low is not None else high
        high = high if high is not None else low
        basis = "stated in notice"
    paid_today = status == "in force" and m.strength >= MIN_STRENGTH_FOR_TODAY
    p_now = scoring.likelihood_now(t, as_of)
    probability = 1.0 if paid_today else round(min(1.0, p_now * m.strength), 4)
    d = duty.Duty(c.authority, float(low), float(high), probability, paid_today, t.doc_id)
    info = {
        "doc_id": t.doc_id, "title": t.title, "url": t.url, "authority": c.authority, "status": status,
        "method": m.method, "strength": m.strength, "reason": m.reason, "llm_reviewed": m.llm_reviewed,
        "likelihood_now": round(p_now, 2), "probability_used": probability,
        "rate_low_pct": float(low), "rate_high_pct": float(high), "rate_basis": basis,
        "counted_in_today": paid_today,
    }
    return d, info


def analyze_product(con, p: Product, threats: list[Threat], as_of: date, use_llm: bool,
                    lookup_base: bool = True, log=print) -> dict:
    base = collector.fetch_hts_rate(con, p.hts_code, log) if lookup_base else None
    if base is None:
        base_pct, base_note = 0.0, "base duty not found; treated as 0%"
        base_raw, base_url = None, None
    elif base["general_pct"] is None:
        base_pct, base_note = 0.0, f"base duty '{base['general_raw']}' is not ad valorem; treated as 0%"
        base_raw, base_url = base["general_raw"], base["source_url"]
    else:
        base_pct, base_note = base["general_pct"], None
        base_raw, base_url = base["general_raw"], base["source_url"]

    duties, infos = [], []
    for t, m in matcher.match_all(p, threats, con, use_llm, log):
        d, info = _duty_for(t, m, as_of)
        duties.append(d)
        infos.append(info)
    scen = duty.scenario_rates(base_pct, duties)
    costs = duty.cost_table(p.unit_cost_usd, p.annual_units, scen)
    pending = [i for i in infos if not i["counted_in_today"]]
    score = round(100 * max((i["probability_used"] for i in pending), default=0.0))
    infos.sort(key=lambda i: (i["counted_in_today"], -i["probability_used"]))
    if pending:
        top = max(pending, key=lambda i: i["probability_used"])
        reason = f"{top['title'][:90]} - {top['reason']}"
    elif infos:
        reason = "Only duties already in force match this product; no pending change found."
    else:
        reason = "No current tariff notice matches this product's HTS code or origin."
    return {
        "sku": p.sku, "description": p.description, "hts_code": matcher.fmt_hts(p.hts_code),
        "country_of_origin": p.country_of_origin, "unit_cost_usd": p.unit_cost_usd, "annual_units": p.annual_units,
        "base_duty": {"pct": base_pct, "raw": base_raw, "source_url": base_url, "note": base_note},
        "threats": infos, "costs": costs, "exposure_score": score, "reason": reason,
    }


def analyze(con, products: list[Product], as_of: date, use_llm: bool, threats: list[Threat] | None = None,
            lookup_base: bool = True, log=print) -> list[dict]:
    """Analyze every product; sorted by likely $ impact per year, then worst case."""
    threats = threats if threats is not None else scoring.load_threats(con)
    act = active_threats(threats, as_of)
    rows = [analyze_product(con, p, act, as_of, use_llm, lookup_base, log) for p in products]
    rows.sort(key=lambda r: (-r["costs"]["likely_delta_per_year"], -r["costs"]["worst_delta_per_year"]))
    return rows
