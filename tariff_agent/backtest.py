"""Step 7 - backtest against real 2025-2026 tariff changes.

For each event the agent is shown only Federal Register documents published before the
change. We record whether it had flagged the affected products through a pending notice,
how many weeks earlier, and what likelihood it gave. A separate scan counts false alarms:
moments when a pending measure reached a 50% likelihood and no duty followed within 90 days.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from . import config, matcher, scoring, simulator
from .schemas import PENDING_STATUSES

ALARM_THRESHOLD = 0.5
NOT_YET = PENDING_STATUSES + ("action announced",)


def _in_group(t: scoring.Threat, authority: str, topics: list[str]) -> bool:
    return t.card.authority == authority and bool(set(t.topics) & set(topics))


def run_event(con, event: dict, threats: list[scoring.Threat], products: dict[str, matcher.Product], use_llm: bool) -> dict:
    row = con.execute("SELECT signing_date, publication_date, html_url, title FROM documents WHERE doc_id=?",
                      (event["action_doc"],)).fetchone()
    change = date.fromisoformat(event["change_date"])
    as_of = change - timedelta(days=1)
    before = [t for t in threats if t.pub < change]          # strictly earlier documents only
    active = simulator.active_threats(before, as_of)

    flagged: dict[str, dict] = {}
    for sku in event["skus"]:
        p = products.get(sku)
        if p is None:
            continue
        for t, m in matcher.match_all(p, active, con, use_llm, log=lambda *_: None):
            if _in_group(t, event["authority"], event["topics"]) and scoring.effective_status(t, as_of) in NOT_YET:
                flagged[sku] = {"doc_id": t.doc_id, "method": m.method, "strength": m.strength, "reason": m.reason}
                break

    # Earliest pending notice on this measure that was already public.
    warnings = sorted((t for t in before if _in_group(t, event["authority"], event["topics"])
                       and not t.procedural and t.card.status in NOT_YET), key=lambda t: t.publication_date)
    # Only count warnings since the last duty decision on the same measure (a new cycle).
    decided = [t for t in before if _in_group(t, event["authority"], event["topics"]) and scoring.is_tariff_action(t)]
    if decided:
        last = max(t.publication_date for t in decided)
        warnings = [w for w in warnings if w.publication_date > last]
    first = warnings[0] if warnings and flagged else None
    latest = warnings[-1] if warnings and flagged else None

    result = {
        "id": event["id"], "name": event["name"], "authority": event["authority"], "outcome": event["outcome"],
        "change_date": event["change_date"], "action_url": row["html_url"] if row else None,
        "action_title": row["title"] if row else None,
        "docs_available": len(before), "skus": event["skus"], "skus_flagged": sorted(flagged),
        "flagged": bool(flagged) and first is not None, "matches": flagged,
        "first_warning": None, "lead_weeks": None, "likelihood_first": None, "likelihood_eve": None,
    }
    if first:
        result.update({
            "first_warning": {"doc_id": first.doc_id, "title": first.title, "url": first.url, "date": first.publication_date},
            "lead_weeks": round((change - first.pub).days / 7, 1),
            "likelihood_first": round(first.card.likelihood_90d, 2),
            "likelihood_eve": round(scoring.likelihood_now(latest, as_of), 2),
        })
    if event["outcome"] == "duty":
        result["verdict"] = "hit" if result["flagged"] else "miss"
    else:
        result["verdict"] = "flagged, no duty followed" if result["flagged"] else "correctly quiet"
    return result


def alarm_scan(threats: list[scoring.Threat], today: date) -> list[dict]:
    """First date each pending measure reached the alarm threshold, and what happened next."""
    groups: dict[tuple[str, str], list[scoring.Threat]] = {}
    for t in threats:
        if t.procedural:
            continue
        for topic in t.topics:
            groups.setdefault((t.card.authority, topic), []).append(t)
    alarms = []
    for (authority, topic), members in sorted(groups.items()):
        members.sort(key=lambda t: t.publication_date)
        d = members[0].pub
        while d <= today:
            visible = [m for m in members if m.pub <= d]
            latest = visible[-1]
            status = scoring.effective_status(latest, d)
            p = scoring.likelihood_now(latest, d)
            if status in PENDING_STATUSES and p >= ALARM_THRESHOLD:
                later = [m for m in members if m.pub > latest.pub]
                action = next((m for m in later if scoring.is_tariff_action(m)), None)
                closed = next((m for m in later if not scoring.is_tariff_action(m)
                               and m.card.status not in ("comments open", "hearing")), None)
                if action and (action.pub - d).days <= 90:
                    verdict = "confirmed"
                elif d + timedelta(days=90) > today:
                    verdict = "open"
                else:
                    verdict = "false alarm"
                note = ""
                if verdict == "false alarm":
                    if action:
                        note = f"duty came later, {round((action.pub - d).days / 7)} weeks after the alarm"
                    elif closed:
                        note = f"later notice imposed no duty: {closed.title[:80]}"
                    else:
                        note = "no decision published yet"
                alarms.append({
                    "authority": authority, "topic": topic, "alarm_date": d.isoformat(), "likelihood": round(p, 2),
                    "notice": latest.title, "url": latest.url, "verdict": verdict, "note": note,
                    "action_date": action.publication_date if action else None,
                })
                # One alarm per notice: a notice covering several topics is merged.
                dup = next((a for a in alarms[:-1] if a["url"] == latest.url), None)
                if dup:
                    alarms.pop()
                    if topic not in dup["topic"].split(", "):
                        dup["topic"] += ", " + topic
                # Resume scanning only after the next notice on this measure.
                nxt = next((m.pub for m in later), None)
                if nxt is None:
                    break
                d = nxt
                continue
            d += timedelta(days=7)
    return alarms


def run(con, as_of: date, use_llm: bool, log=print) -> dict:
    log("Step 7 - backtest")
    events = json.loads(config.BACKTEST_EVENTS.read_text(encoding="utf-8"))["events"]
    products = {p.sku: p for p in matcher.load_products(config.SAMPLE_IMPORTER.read_text(encoding="utf-8"))}
    threats = scoring.load_threats(con)
    extractors = sorted({t.extractor for t in threats})
    results = [run_event(con, e, threats, products, use_llm) for e in events]
    alarms = alarm_scan(threats, as_of)

    duty_events = [r for r in results if r["outcome"] == "duty"]
    hits = [r for r in duty_events if r["verdict"] == "hit"]
    closed = [a for a in alarms if a["verdict"] != "open"]
    summary = {
        "as_of": as_of.isoformat(), "extractors": extractors, "events": len(results),
        "duty_events": len(duty_events), "hits": len(hits),
        "hit_rate": round(len(hits) / len(duty_events), 2) if duty_events else None,
        "avg_lead_weeks": round(sum(r["lead_weeks"] for r in hits) / len(hits), 1) if hits else None,
        "avg_likelihood_eve": round(sum(r["likelihood_eve"] for r in hits) / len(hits), 2) if hits else None,
        "alarms": len(alarms), "alarms_confirmed": sum(a["verdict"] == "confirmed" for a in alarms),
        "false_alarms": sum(a["verdict"] == "false alarm" for a in alarms),
        "alarms_open": sum(a["verdict"] == "open" for a in alarms),
        "alarm_precision": round(sum(a["verdict"] == "confirmed" for a in closed) / len(closed), 2) if closed else None,
    }
    for r in results:
        lead = f"{r['lead_weeks']} wk early" if r["lead_weeks"] is not None else "no advance notice"
        log(f"  {r['verdict']:26} {lead:18} {r['name'][:70]}")
    log(f"  hit rate {summary['hits']}/{summary['duty_events']}, avg lead {summary['avg_lead_weeks']} weeks, "
        f"false alarms {summary['false_alarms']} of {summary['alarms']} alarms")
    return {"summary": summary, "events": results, "alarms": alarms}


def to_markdown(bt: dict) -> str:
    s = bt["summary"]
    mode = "LLM reader" if s["extractors"] == ["llm"] else (
        "offline rule-based reader (no API key was available for this run)" if s["extractors"] == ["heuristic"]
        else "mixed: " + ", ".join(s["extractors"]))
    L = [
        "# Backtest report",
        "",
        f"_Generated by `python run.py` on {s['as_of']}. Notice reader used: **{mode}**._",
        "",
        f"> {config.ADVISORY}",
        "",
        "## Method",
        "",
        "For each real tariff change below, the agent was given only Federal Register documents published "
        "**before** the date the change was signed (or, for agency notices, took effect). An event is a **hit** "
        "when, on the day before the change, a pending notice on the same measure matched at least one of the "
        "affected sample products. Lead time runs from the first such pending notice to the change date. "
        "\"Likelihood at first warning\" is the reader's 90-day estimate on that first notice; \"likelihood on the "
        "eve\" is the aged estimate the day before the change (see `tariff_agent/scoring.py`).",
        "",
        "## Headline numbers",
        "",
        f"- **Hit rate:** {s['hits']} of {s['duty_events']} duty changes flagged in advance"
        + (f" ({s['hit_rate']:.0%})" if s["hit_rate"] is not None else ""),
        f"- **Average lead time (hits):** {s['avg_lead_weeks']} weeks" if s["avg_lead_weeks"] is not None else "- **Average lead time:** n/a",
        f"- **Average likelihood on the eve of the change (hits):** {s['avg_likelihood_eve']:.0%}" if s["avg_likelihood_eve"] is not None else "",
        f"- **False alarms:** {s['false_alarms']} of {s['alarms']} high-likelihood alarms (>= {ALARM_THRESHOLD:.0%}) were not followed "
        f"by a duty within 90 days; {s['alarms_confirmed']} were confirmed; {s['alarms_open']} are still inside their 90-day window",
        "",
        "## Event by event",
        "",
        "| Event | Change date | Verdict | Products flagged | First warning | Lead (weeks) | Likelihood at first warning | Likelihood on the eve |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in bt["events"]:
        fw = f"[{r['first_warning']['date']}]({r['first_warning']['url']})" if r["first_warning"] else "none"
        pct = lambda v: f"{v:.0%}" if v is not None else "-"  # noqa: E731
        L.append(f"| [{r['name']}]({r['action_url']}) | {r['change_date']} | **{r['verdict']}** | "
                 f"{len(r['skus_flagged'])}/{len(r['skus'])} | {fw} | {r['lead_weeks'] if r['lead_weeks'] is not None else '-'} | "
                 f"{pct(r['likelihood_first'])} | {pct(r['likelihood_eve'])} |")
    L += ["", "## Misses and surprises", ""]
    misses = [r for r in bt["events"] if r["verdict"] == "miss"]
    if not misses:
        L.append("None in this run.")
    for r in misses:
        L.append(f"- **{r['name']}** ({r['change_date']}): no pending Federal Register notice on this measure existed before the "
                 f"change among the {r['docs_available']} documents available, so there was nothing to flag. "
                 "This kind of change is announced and takes effect within days; a notice-based agent cannot see it coming.")
    nod = [r for r in bt["events"] if r["outcome"] == "no_duty"]
    for r in nod:
        L.append(f"- **{r['name']}** ({r['change_date']}): {r['verdict']}. The agent had flagged "
                 f"{len(r['skus_flagged'])}/{len(r['skus'])} products"
                 + (f" from {r['first_warning']['date']}" if r["first_warning"] else "")
                 + ", and the investigation ended without a duty. An importer who acted on the flag would have prepared for a tariff that did not arrive.")
    L += ["", "## False-alarm scan", "",
          f"Every measure in the database was replayed week by week. An alarm is the first week a pending measure's "
          f"likelihood reached {ALARM_THRESHOLD:.0%}.", "",
          "| Alarm date | Measure | Likelihood | Outcome | Note |", "|---|---|---|---|---|"]
    for a in bt["alarms"]:
        L.append(f"| {a['alarm_date']} | [Section {a['authority']}: {a['topic'].replace('_', ' ')}]({a['url']}) | "
                 f"{a['likelihood']:.0%} | {a['verdict']} | {a['note']} |")
    if not bt["alarms"]:
        L.append("| - | no alarms | - | - | - |")
    L += [
        "", "## How to read these results", "",
        "- **Lead time is long but early likelihoods are low.** Investigations are public for months, so the agent "
        "flags products early; but a freshly opened investigation rarely ends within 90 days, so the first-warning "
        "likelihood is deliberately small. The useful signal is the flag plus a likelihood that rises as statutory "
        "deadlines approach.",
        "- **The hazard table is in-sample.** The ageing rule in `scoring.py` was set from the same 2025-2026 "
        "Section 232 investigations that appear in this backtest, so eve-of-change likelihoods and the false-alarm "
        "count are optimistic. A fair test needs future events.",
        "- **Small sample.** Eight events and one sample importer. Treat the hit rate as an illustration, not a statistic.",
        "- **LLM hindsight.** When the LLM reader is used, the model may already know how these events ended. The "
        "prompt restricts it to the notice text, but that cannot be verified from outside.",
        "- **Publication lag.** Proclamations are usually posted by the White House a few days before they appear in "
        "the Federal Register; dates here use the signing date for the change and Federal Register publication dates "
        "for what the agent could see.",
        "",
    ]
    return "\n".join(x for x in L if x is not None)
