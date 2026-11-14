"""Step 6 - weekly alert: a short plain-English brief with a source URL on every claim."""
from __future__ import annotations

import json
from datetime import date

from . import config, llm
from .schemas import AlertItem, WeeklyAlert

PROMPT_VERSION = "alert-v1"

SYSTEM = """You write a short weekly tariff brief for the owner of a small US importing business.

You receive JSON with the importer's most exposed products, the tariff notices behind each exposure,
and upcoming dates. Write in plain English for a busy non-lawyer. Be specific about products (use the
SKU), dollars and dates. Use only facts in the input; do not add outside knowledge, rates or dates.

Return one JSON object and nothing else:
{
  "summary": "2-3 sentences on what matters most this week",
  "top_risks": [{"headline": "...", "detail": "...", "source_urls": ["..."]}],
  "dates_to_watch": [{"headline": "...", "detail": "...", "source_urls": ["..."]}],
  "suggested_actions": [{"headline": "...", "detail": "...", "source_urls": ["..."]}]
}

Rules:
- At most 5 items per list. Every item must cite at least one source URL copied exactly from the input
  (the notice that supports the claim). Never cite a URL that is not in the input.
- Suggested actions are practical options to consider, such as ordering early before an effective date,
  quoting a second supplier in another country, using a foreign-trade zone or bonded warehouse to defer
  duty, or repricing. Tie each one to a specific product and notice.
- Where the input marks a rate as assumed or a match as unverified, say so.
- Do not give legal advice or tell the reader a classification is certain."""


def _fmt_money(x: float) -> str:
    return f"${x:,.0f}"


def build_context(products: list[dict], timeline: list[dict], as_of: date, top_n: int = 8) -> dict:
    top = [p for p in products if p["costs"]["worst_delta_per_year"] > 0][:top_n]
    return {
        "as_of": as_of.isoformat(),
        "products": [{
            "sku": p["sku"], "description": p["description"], "hts_code": p["hts_code"],
            "country_of_origin": p["country_of_origin"],
            "duty_today_pct": p["costs"]["today_pct"], "duty_likely_pct": round(p["costs"]["likely_pct"], 1),
            "duty_worst_pct": p["costs"]["worst_pct"],
            "likely_extra_cost_per_year_usd": p["costs"]["likely_delta_per_year"],
            "worst_extra_cost_per_year_usd": p["costs"]["worst_delta_per_year"],
            "exposure_score": p["exposure_score"],
            "notices": [{
                "title": t["title"], "url": t["url"], "authority": f"Section {t['authority']}",
                "status": t["status"], "likelihood_within_90_days": t["likelihood_now"],
                "rate_range_pct": [t["rate_low_pct"], t["rate_high_pct"]], "rate_basis": t["rate_basis"],
                "match_reason": t["reason"], "already_paid_today": t["counted_in_today"],
            } for t in p["threats"]],
        } for p in top],
        "upcoming_dates": [{k: e[k] for k in ("date", "label", "basis", "title", "url", "days_away")} for e in timeline[:12]],
    }


def offline_alert(products: list[dict], timeline: list[dict], as_of: date) -> WeeklyAlert:
    """Template brief used when no LLM is available. Same structure, same citation rule."""
    exposed = [p for p in products if p["costs"]["likely_delta_per_year"] > 0]
    risks, actions, dates = [], [], []
    for p in exposed[:5]:
        pend = [t for t in p["threats"] if not t["counted_in_today"]]
        if not pend:
            continue
        t = max(pend, key=lambda x: x["probability_used"])
        c = p["costs"]
        basis = " The rate is an assumed scenario range because the notice states none." if "assumed" in t["rate_basis"] else ""
        risks.append(AlertItem(
            headline=f"{p['sku']} ({p['description']}): about {_fmt_money(c['likely_delta_per_year'])} more per year in the likely case",
            detail=(f"Duty today {c['today_pct']:.1f}%, likely {c['likely_pct']:.1f}%, worst case {c['worst_pct']:.1f}% "
                    f"(up to {_fmt_money(c['worst_delta_per_year'])} more per year). Main driver: Section {t['authority']} notice "
                    f"\"{t['title'][:110]}\" ({t['status']}, {t['likelihood_now']:.0%} chance within 90 days). {t['reason']}{basis}"),
            source_urls=[t["url"]]))
    for e in timeline[:5]:
        dates.append(AlertItem(
            headline=f"{e['date']} ({e['days_away']} days): {e['label']}",
            detail=f"{e['title'][:140]} [{e['basis']}].", source_urls=[e["url"]]))
    seen = set()
    for p in exposed:
        for t in p["threats"]:
            if t["counted_in_today"] or (p["sku"], "x") in seen:
                continue
            c = p["costs"]
            if t["status"] == "action announced" and t["likelihood_now"] >= 0.9:
                actions.append(AlertItem(
                    headline=f"Order early: {p['sku']}",
                    detail=(f"A Section {t['authority']} duty of {t['rate_low_pct']:.0f}-{t['rate_high_pct']:.0f}% has been announced and is not yet "
                            f"in effect. Consider pulling forward orders so goods clear customs before the effective date, and ask your broker "
                            f"whether a foreign-trade zone or bonded warehouse would let you defer the duty."),
                    source_urls=[t["url"]]))
                seen.add((p["sku"], "x"))
            elif t["likelihood_now"] >= 0.2 and c["worst_delta_per_unit"] > 0.05 * p["unit_cost_usd"]:
                actions.append(AlertItem(
                    headline=f"Quote a second supplier and review pricing: {p['sku']}",
                    detail=(f"A pending Section {t['authority']} measure could raise landed cost by up to "
                            f"${c['worst_delta_per_unit']:,.2f} per unit ({c['worst_delta_per_unit'] / max(p['unit_cost_usd'], 1e-9):.0%} of unit cost). "
                            f"Get a quote from a supplier outside the measure's scope and decide how much of an increase you could pass on."),
                    source_urls=[t["url"]]))
                seen.add((p["sku"], "x"))
            if len(actions) >= 5:
                break
        if len(actions) >= 5:
            break
    total_likely = sum(p["costs"]["likely_delta_per_year"] for p in products)
    total_worst = sum(p["costs"]["worst_delta_per_year"] for p in products)
    summary = (f"As of {as_of.isoformat()}, {len(exposed)} of {len(products)} products have a pending tariff change that could raise "
               f"their duty. Across the catalogue the likely added cost is about {_fmt_money(total_likely)} per year and the worst "
               f"case about {_fmt_money(total_worst)} per year. This brief was produced by the offline template because no LLM was available.")
    return WeeklyAlert(summary=summary, top_risks=risks, dates_to_watch=dates, suggested_actions=actions)


def _allowed_urls(ctx: dict) -> set[str]:
    urls = {n["url"] for p in ctx["products"] for n in p["notices"]}
    return urls | {e["url"] for e in ctx["upcoming_dates"]}


def _citations_ok(alert: WeeklyAlert, allowed: set[str]) -> bool:
    items = alert.top_risks + alert.dates_to_watch + alert.suggested_actions
    return all(i.source_urls and set(i.source_urls) <= allowed for i in items)


def write_alert(con, products: list[dict], timeline: list[dict], as_of: date, use_llm: bool, log=print) -> tuple[WeeklyAlert, str]:
    """Return (alert, writer) where writer is 'llm' or 'template'."""
    ctx = build_context(products, timeline, as_of)
    if use_llm and ctx["products"]:
        allowed = _allowed_urls(ctx)
        user = "INPUT JSON:\n" + json.dumps(ctx, indent=1)
        for version, extra in ((PROMPT_VERSION, ""), (PROMPT_VERSION + "-retry", "\n\nYour previous brief cited a URL that is not in the input. Cite only URLs that appear in the input JSON.")):
            try:
                alert = llm.call_json("weekly_alert", f"alert-{as_of.isoformat()}", SYSTEM, user + extra, WeeklyAlert, con,
                                      prompt_version=version)
            except Exception as exc:
                log(f"  ! alert LLM call failed ({type(exc).__name__}: {exc}); using template")
                break
            if _citations_ok(alert, allowed):
                return alert, "llm"
            log("  ! alert cited a URL outside the input; retrying once")
    return offline_alert(products, timeline, as_of), "template"


def to_markdown(alert: WeeklyAlert, as_of: date, writer: str) -> str:
    def section(title: str, items: list[AlertItem]) -> str:
        if not items:
            return f"## {title}\n\nNothing to report this week.\n"
        lines = [f"## {title}\n"]
        for i in items:
            cites = " ".join(f"[source]({u})" for u in i.source_urls)
            lines.append(f"- **{i.headline}**  \n  {i.detail} {cites}")
        return "\n".join(lines) + "\n"

    return "\n".join([
        f"# Weekly tariff alert - {as_of.isoformat()}",
        "",
        f"> {config.ADVISORY}",
        "",
        alert.summary,
        "",
        section("Top risks", alert.top_risks),
        section("Dates to watch", alert.dates_to_watch),
        section("Suggested actions to consider", alert.suggested_actions),
        f"_Written by: {'Claude (' + config.MODEL + ')' if writer == 'llm' else 'offline template (no LLM)'}. "
        "Every item links to the Federal Register notice it is based on._",
        "",
    ])
