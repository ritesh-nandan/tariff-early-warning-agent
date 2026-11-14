"""Offline, rule-based notice reader.

Used only when no Anthropic credentials are configured (or TARIFF_OFFLINE=1), so the
pipeline, tests and dashboard still run end to end. It reads titles, Federal Register
metadata and a few regular expressions. It is deliberately conservative - anything it
cannot find stays null - and noticeably weaker than the LLM reader at rates, scope and
country lists. Cards it produces are labelled extractor="heuristic" everywhere.
"""
from __future__ import annotations

import re
from datetime import datetime

from .schemas import KeyDates, OtherDate, ThreatCard

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_RX = rf"((?:{MONTHS}) \d{{1,2}}, \d{{4}})"

COUNTRIES = [
    "China", "Brazil", "Nicaragua", "Vietnam", "Germany", "Canada", "Mexico", "Japan",
    "United Kingdom", "Russia", "Russian Federation", "Taiwan", "India", "Cuba", "Iran", "Venezuela",
]
BROAD_301 = re.compile(r"various economies|certain economies|each economy", re.I)


def _iso(s: str) -> str | None:
    try:
        return datetime.strptime(s, "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def _authority(doc: dict, text: str) -> str:
    """Title cues first; otherwise the statute the text cites most often."""
    title = doc["title"]
    if re.search(r"temporary import surcharge", title, re.I):
        return "122"
    if re.search(r"section 301", title, re.I) or doc.get("query") == "ustr_301":
        return "301"
    if re.search(r"section 232|adjust(?:ing)? imports|tariff regimes for imports|threat to national security from imports",
                 title, re.I) or doc.get("query") == "bis_232":
        return "232"
    counts = {
        "232": len(re.findall(r"section 232", text, re.I)),
        "301": len(re.findall(r"section 301", text, re.I)),
        "122": len(re.findall(r"section 122", text, re.I)),
        "other": len(re.findall(r"IEEPA|International Emergency Economic Powers Act", text)),
    }
    best = max(counts, key=counts.get)
    return best if counts[best] >= 3 else "other"


def _operative(text: str) -> str:
    """The operative part of a presidential document (after the recitals), if marked."""
    m = re.search(r"do hereby proclaim|proclaim as follows|it is hereby ordered|hereby ordered", text, re.I)
    return text[m.start():] if m else text


def _effective_dates(text: str) -> list[str]:
    hits = re.findall(rf"on or after 12:01 a\.m\.[^.;]{{0,60}}? on {DATE_RX}", text)
    out: list[str] = []
    for h in hits:
        iso = _iso(h)
        if iso and iso not in out:
            out.append(iso)
    return sorted(out)


def _rates(text: str) -> tuple[float | None, float | None]:
    """Duty rates stated as '<n> percent ad valorem', 'a <n> percent ... duty/tariff', etc."""
    pats = [
        r"(\d+(?:\.\d+)?) percent ad valorem",
        r"(\d+(?:\.\d+)?) percent (?:import )?(?:dut(?:y|ies)|tariff|rate)",
        r"(\d+(?:\.\d+)?) percent is the appropriate rate",
        r"shall be:? (?:\(a\) )?(\d+(?:\.\d+)?) percent",
        r"(?:proposes|imposing|impose) (?:an? )?(?:additional )?(\d+(?:\.\d+)?)\s?(?:%|percent) as the rate",
        r"(\d+(?:\.\d+)?)% as the rate of additional duties",
    ]
    vals = sorted({float(v) for p in pats for v in re.findall(p, text, flags=re.I) if 0 < float(v) <= 400})
    return (vals[0], vals[-1]) if vals else (None, None)


def _hts_codes(text: str) -> list[str]:
    codes = re.findall(r"\b(\d{4}\.\d{2}(?:\.\d{2}(?:\.?\d{2})?)?)\b", text)
    codes += [f"{c}" for c in re.findall(r"\bheadings? (\d{4})\b", text)]
    out: list[str] = []
    for c in codes:
        digits = re.sub(r"\D", "", c)
        if digits[:2] in ("98", "99") or digits in out:
            continue
        out.append(digits)
    return out[:60]


def _countries(doc: dict, authority: str) -> list[str]:
    title = doc["title"]
    if authority in ("232", "122") or BROAD_301.search(title):
        return ["ALL"]
    found = [c for c in COUNTRIES if re.search(rf"\b{c}\b|\b{c}'s\b|\b{c}n\b", title)]
    if "Russian Federation" in found:
        found = [c for c in found if c != "Russia"]
    return found


def _categories(title: str) -> list[str]:
    m = re.search(r"(?:Imports of|Investigation of(?: Imports of)?|Adjusting Imports of|Tariff Regimes for Imports of)\s+(.+?)(?: Into the United States|$)", title)
    if not m:
        return []
    raw = re.sub(r"\band\b", ",", m.group(1))
    return [p.strip(" ,.") for p in raw.split(",") if len(p.strip(" ,.")) > 2][:8]


def read_notice(doc: dict) -> ThreatCard:
    """Build a threat card from one document row using rules only."""
    title = doc["title"]
    text = " ".join((doc["text"] or "").split())  # undo Federal Register line wrapping
    text = re.sub(r"\[\[Page \d+\]\] ?", "", text)
    authority = _authority(doc, text)
    presidential = (doc.get("doc_type") or "").startswith("Presidential")
    eff = _effective_dates(text)
    low, high = (None, None)
    hts: list[str] = []
    dates = KeyDates(comment_deadline=doc.get("comments_close_on"))
    other: list[OtherDate] = []
    notes: list[str] = []

    if re.search(r"ending certain tariff actions", title, re.I):
        status, p = "struck down", 0.0
        notes.append("Order ends previously imposed duties")
    elif presidential and re.search(r"addressing the threat to national security from imports|through section 232 actions", title, re.I):
        status, p = "investigation", 0.05
        notes.append("Presidential order directing a Section 232 investigation")
    elif presidential:
        low, high = _rates(_operative(text))  # recitals quote historical rates; skip them
        hts = _hts_codes(text)
        if eff or low is not None:
            pub = doc.get("publication_date") or ""
            status = "in force" if eff and eff[0] <= pub else "action announced"
            p = 1.0 if status == "in force" else 0.9
            notes.append("Presidential action stating duty rates and/or an effective date")
        else:
            status, p = "investigation", 0.10
            notes.append("Presidential document with no duty rate or effective date found in its text "
                         "(for example, a direction to negotiate or monitor)")
    elif re.search(r"request for public comments on section 232", title, re.I):
        status, p = "comments open", 0.05
        hts = _hts_codes(text)
        m = re.search(rf"On {DATE_RX}, the Secretary of Commerce initiated", text)
        if m:
            dates.initiated = _iso(m.group(1))
        notes.append("Commerce opened a Section 232 investigation and requested comments; "
                     "2025-2026 investigations took 5-15 months to reach a proclamation")
    elif re.search(r"^initiation of", title, re.I):
        status = "hearing" if re.search(r"hearing", title, re.I) else "comments open"
        p = 0.10
        m = re.search(rf"{DATE_RX}: The (?:U\.S\. |United States )?Trade Representative initiated", text) \
            or re.search(rf"On {DATE_RX}, the (?:U\.S\. |United States )?Trade Representative initiated", text)
        if m:
            dates.initiated = _iso(m.group(1))
        notes.append("USTR initiated a Section 301 investigation")
    elif re.search(r"^notice of (?:implementation of )?actions?\b|^notice of modification|^continuation of actions", title, re.I):
        low, high = _rates(text)
        hts = _hts_codes(text)
        pub = doc.get("publication_date") or ""
        status = "in force" if eff and eff[0] <= pub else "action announced"
        p = 1.0 if status == "in force" else 0.9
        notes.append("USTR notice of action")
    elif re.search(r"proposed (?:action|modification|implementation of duties)|"
                   r"determinations? and request for comments concerning actions?", title, re.I):
        status, p = "comments open", 0.60
        low, high = _rates(text)
        hts = _hts_codes(text)
        notes.append("Agency has formally proposed a duty action and is taking comments")
    elif re.search(r"request for (?:public )?comments", title, re.I):
        status, p = "comments open", 0.10
        notes.append("Agency is taking comments (for example on exclusions, a suspension or a review); no duty increase proposed in the title")
    else:
        status, p = ("in force", 1.0) if doc.get("doc_type") == "Rule" else ("investigation", 0.05)
        notes.append("Implementing or administrative notice")

    if eff:
        dates.effective_date = eff[0]
        other += [OtherDate(label="Later effective date (staged)", date=d) for d in eff[1:4]]
    if authority == "122":
        m = re.search(rf"continue in effect through .{{0,60}}? on {DATE_RX}", text)
        if m:
            dates.statutory_deadline = _iso(m.group(1))

    broad = authority == "122" or bool(BROAD_301.search(title)) or bool(re.search(r"reciprocal tariff", title, re.I))
    return ThreatCard(
        authority=authority, status=status, affected_categories=_categories(title), hts_codes=hts,
        countries=_countries(doc, authority), applies_to_all_products=broad,
        rate_low_pct=low, rate_high_pct=high, key_dates=dates, other_dates=other,
        likelihood_90d=p, rationale="; ".join(notes) + " (rule-based reading, no LLM).",
        source_url=doc["html_url"],
    )
