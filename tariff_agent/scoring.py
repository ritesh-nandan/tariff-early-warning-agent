"""Deterministic state and likelihood logic (no LLM).

A threat card describes one notice as of its publication date. This module answers the
"as of today" questions the rest of the pipeline needs:
  * which card is the current state of each topic (later notices supersede earlier ones);
  * whether an announced action is in force yet, or has lapsed;
  * how the 90-day likelihood of a pending action should age as the months pass.
All functions take an explicit `as_of` date so the backtest can replay history.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import concordance
from .schemas import PENDING_STATUSES, ThreatCard

# Notices that implement or administer an existing action rather than change tariffs.
PROCEDURAL_TITLE = re.compile(
    r"Inclusions Process|Procedures (?:To|for)|Guidance and Procedures|Exclusion|^Implementation of Duties|"
    r"Conforming Amendment|Stockpiling|Technical Correction|Delegation of Authority",
    re.I,
)

# 90-day hazard for a pending investigation, by days since initiation: the share of
# 2025-2026 Section 232 investigations in this database that ended in a proclamation
# within the following 90 days, given they were still open at that point (11 cases;
# see reports/backtest.md). In-sample and coarse by design - a prior, not a forecast.
HAZARD_232 = [(0, 0.05), (90, 0.20), (330, 0.50), (360, 0.75)]
# Section 301: USTR must decide within 12 months of initiation (19 U.S.C. 2414).
HAZARD_301 = [(0, 0.10), (60, 0.25), (270, 0.50)]
HAZARD_OTHER = [(0, 0.10), (180, 0.20)]
EMERGENCY_POWER_TOPICS = {"reciprocal_tariffs", "fentanyl_border_duties", "ieepa_tariff_actions"}
FRESH_DAYS = 45         # how long a notice's own likelihood estimate is trusted as-is
SEC232_REPORT_DAYS = 270  # 19 U.S.C. 1862(b)(3)(A): Commerce report due 270 days after initiation


@dataclass
class Threat:
    """A threat card plus its document metadata."""

    doc_id: str
    title: str
    publication_date: str
    url: str
    card: ThreatCard
    extractor: str = "heuristic"
    topics: list[str] = field(default_factory=list)
    clock_start: str | None = None  # when the current investigation cycle on this measure began

    @property
    def procedural(self) -> bool:
        return bool(PROCEDURAL_TITLE.search(self.title))

    @property
    def pub(self) -> date:
        return date.fromisoformat(self.publication_date)


def load_threats(con, before: str | None = None) -> list[Threat]:
    """All threat cards, optionally only for documents published strictly before `before`."""
    q = ("SELECT d.doc_id, d.title, d.publication_date, d.html_url, t.card_json, t.extractor "
         "FROM threat_cards t JOIN documents d USING(doc_id)")
    args: tuple = ()
    if before:
        q += " WHERE d.publication_date < ?"
        args = (before,)
    out = []
    for r in con.execute(q + " ORDER BY d.publication_date", args):
        card = ThreatCard.model_validate(json.loads(r["card_json"]))
        out.append(Threat(r["doc_id"], r["title"], r["publication_date"], r["html_url"], card,
                          r["extractor"], concordance.topics_for_notice(r["title"], card.affected_categories)))
    set_clock_starts(out)
    return out


def set_clock_starts(threats: list[Threat]) -> None:
    """Give each pending notice the start date of its investigation cycle.

    A follow-up notice (say, a proposed action) often omits the initiation date. The clock
    for the hazard table should still run from the first pending notice on that measure
    since the last duty decision, so the start date is carried forward within the group.
    """
    groups: dict[tuple[str, str], list[Threat]] = {}
    for t in threats:
        for topic in t.topics:
            groups.setdefault((t.card.authority, topic), []).append(t)
    for members in groups.values():
        start: str | None = None
        for t in sorted(members, key=lambda m: (m.publication_date, m.doc_id)):
            if t.procedural:
                continue
            if t.card.status in PENDING_STATUSES:
                start = start or t.card.key_dates.initiated or t.publication_date
                t.clock_start = min(t.clock_start, start) if t.clock_start else start
            else:
                start = None


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def effective_dates(card: ThreatCard) -> list[date]:
    out = []
    if card.key_dates.effective_date:
        out.append(_d(card.key_dates.effective_date))
    out += [_d(o.date) for o in card.other_dates if "effective" in o.label.lower()]
    return sorted(out)


def effective_status(t: Threat, as_of: date) -> str:
    """Status as of `as_of`: announced actions become in force; time-limited ones lapse."""
    card = t.card
    if card.status not in ("action announced", "in force"):
        return card.status
    eff = effective_dates(card)
    if card.status == "in force" and not eff:
        status = "in force"
    elif eff and eff[0] <= as_of:
        status = "in force"
    else:
        status = "action announced"
    # Section 122 surcharges are capped at 150 days unless extended by Congress.
    end = _d(card.key_dates.statutory_deadline)
    if status == "in force" and card.authority == "122" and end and end <= as_of:
        return "expired"
    return status


def _hazard(table: list[tuple[int, float]], days: int) -> float:
    p = table[0][1]
    for start, value in table:
        if days >= start:
            p = value
    return p


def likelihood_now(t: Threat, as_of: date) -> float:
    """Probability the action takes effect within 90 days of `as_of`."""
    status = effective_status(t, as_of)
    if status == "in force":
        return 1.0
    if status in ("struck down", "expired"):
        return 0.0
    card = t.card
    if status == "action announced":
        upcoming = [e for e in effective_dates(card) if e > as_of]
        if upcoming:
            return 0.95 if (upcoming[0] - as_of).days <= 90 else 0.30
        return max(card.likelihood_90d, 0.70) if (as_of - t.pub).days <= FRESH_DAYS else 0.50
    # Pending: trust the notice-level estimate while fresh, then age it with the hazard table.
    age = (as_of - t.pub).days
    if age <= FRESH_DAYS:
        return card.likelihood_90d
    start = _d(t.clock_start) or _d(card.key_dates.initiated)
    if start is None:
        if "investigation" not in t.title.lower():
            return min(card.likelihood_90d, 0.10)  # not a dated investigation: nothing to age
        start = t.pub
    table = {"232": HAZARD_232, "301": HAZARD_301}.get(card.authority, HAZARD_OTHER)
    p = _hazard(table, (as_of - start).days)
    # A formally proposed duty stays elevated for six months after the proposal.
    if card.likelihood_90d >= 0.5 and age <= 180:
        p = max(p, 0.5)
    return p


def group_key(t: Threat, topic: str) -> tuple[str, str]:
    return (t.card.authority, topic)


def current_threats(threats: list[Threat], as_of: date) -> list[Threat]:
    """The latest substantive notice per (authority, topic) as of `as_of`."""
    visible = [t for t in threats if t.pub <= as_of]
    # An order ending tariff programmes retires the earlier emergency-powers ("other")
    # measures it revokes; in this dataset that is the February 2026 end of IEEPA duties.
    enders = [t for t in visible if t.card.status == "struck down"]
    if enders:
        cut = max(e.publication_date for e in enders)
        visible = [t for t in visible
                   if t.card.status == "struck down" or t.publication_date > cut
                   or not (t.card.authority == "other" or set(t.topics) & EMERGENCY_POWER_TOPICS)]
    groups: dict[tuple[str, str], list[Threat]] = {}
    for t in visible:
        for topic in (t.topics or [f"doc:{t.doc_id}"]):
            groups.setdefault(group_key(t, topic), []).append(t)
    current: dict[str, Threat] = {}
    order = lambda m: (m.publication_date, bool(effective_dates(m.card)), m.doc_id)  # noqa: E731
    for members in groups.values():
        substantive = [m for m in members if not m.procedural] or members
        latest = max(substantive, key=order)
        current[latest.doc_id] = latest
        # Keep the standing duty too when a newer pending notice sits on top of it.
        decided = [m for m in substantive if m.card.status in ("action announced", "in force", "struck down")]
        if decided and latest.card.status in PENDING_STATUSES:
            standing = max(decided, key=order)
            current[standing.doc_id] = standing
    return sorted(current.values(), key=lambda t: t.publication_date, reverse=True)


def is_tariff_action(t: Threat) -> bool:
    """True for a notice that announces or imposes a duty (not a procedural notice)."""
    return (t.card.status in ("action announced", "in force") and not t.procedural
            and (t.card.rate_high_pct is not None or bool(effective_dates(t.card))))


def upcoming_dates(threats: list[Threat], as_of: date, horizon_days: int = 365) -> list[dict]:
    """Timeline entries: stated key dates plus derived Section 232 report deadlines."""
    out = []
    end = as_of + timedelta(days=horizon_days)
    for t in threats:
        c = t.card
        status = effective_status(t, as_of)
        entries = [
            ("Comment deadline", c.key_dates.comment_deadline, "stated in notice"),
            ("Hearing", c.key_dates.hearing, "stated in notice"),
            ("Statutory deadline / expiry", c.key_dates.statutory_deadline, "stated in notice"),
            ("Duty takes effect", c.key_dates.effective_date, "stated in notice"),
        ] + [(o.label, o.date, "stated in notice") for o in c.other_dates]
        if c.authority == "232" and status in PENDING_STATUSES and c.key_dates.initiated:
            due = _d(c.key_dates.initiated) + timedelta(days=SEC232_REPORT_DAYS)
            entries.append(("Commerce report due (270 days after initiation)", due.isoformat(),
                            "derived from 19 U.S.C. 1862, not stated in notice"))
        seen = set()
        for label, ds, basis in entries:
            if not ds or (label, ds) in seen:
                continue
            seen.add((label, ds))
            when = _d(ds)
            if as_of <= when <= end:
                out.append({
                    "date": ds, "label": label, "basis": basis, "doc_id": t.doc_id,
                    "title": t.title, "url": t.url, "authority": c.authority,
                    "status": status, "days_away": (when - as_of).days,
                })
    return sorted(out, key=lambda e: e["date"])
