"""Pydantic models. Every LLM output is validated against these before use."""
from __future__ import annotations

import re
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

Authority = Literal["232", "301", "122", "other"]
Status = Literal[
    "investigation", "comments open", "hearing", "action announced", "in force", "struck down",
]
PENDING_STATUSES = ("investigation", "comments open", "hearing")

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _iso_or_none(v):
    if v in (None, "", "null"):
        return None
    if isinstance(v, date):
        return v.isoformat()
    v = str(v).strip()
    if not _ISO.match(v):
        raise ValueError(f"date must be YYYY-MM-DD or null, got {v!r}")
    date.fromisoformat(v)
    return v


class KeyDates(BaseModel):
    initiated: Optional[str] = None
    comment_deadline: Optional[str] = None
    hearing: Optional[str] = None
    statutory_deadline: Optional[str] = None
    effective_date: Optional[str] = None

    _v = field_validator("*", mode="before")(_iso_or_none)


class OtherDate(BaseModel):
    label: str
    date: str

    @field_validator("date", mode="before")
    @classmethod
    def _d(cls, v):
        out = _iso_or_none(v)
        if out is None:
            raise ValueError("date required")
        return out


class ThreatCard(BaseModel):
    """Structured summary of one tariff notice. Unknown facts are null, never guessed."""

    authority: Authority
    status: Status
    affected_categories: list[str] = Field(default_factory=list)
    hts_codes: list[str] = Field(default_factory=list, description="HTS chapters/headings/subheadings stated in the notice")
    countries: list[str] = Field(default_factory=list, description='Country names, or ["ALL"] for all origins')
    applies_to_all_products: bool = False
    rate_low_pct: Optional[float] = None
    rate_high_pct: Optional[float] = None
    key_dates: KeyDates = Field(default_factory=KeyDates)
    other_dates: list[OtherDate] = Field(default_factory=list)
    likelihood_90d: float = Field(ge=0.0, le=1.0)
    rationale: str
    source_url: str

    @field_validator("hts_codes", mode="before")
    @classmethod
    def _codes(cls, v):
        out = []
        for c in v or []:
            digits = re.sub(r"\D", "", str(c))
            # Chapters 98/99 are special-provision headings, not product classifications.
            if 2 <= len(digits) <= 10 and digits[:2] not in ("98", "99") and digits not in out:
                out.append(digits)
        return out

    @field_validator("rate_low_pct", "rate_high_pct")
    @classmethod
    def _rate(cls, v):
        if v is not None and not (0 <= v <= 1000):
            raise ValueError("rate must be a percentage between 0 and 1000")
        return v

    @field_validator("rate_high_pct")
    @classmethod
    def _order(cls, v, info):
        low = info.data.get("rate_low_pct")
        if v is not None and low is not None and v < low:
            raise ValueError("rate_high_pct must be >= rate_low_pct")
        return v


class MatchJudgement(BaseModel):
    """LLM verdict on an ambiguous / derivative product-to-threat match."""

    covered: Literal["yes", "likely", "unlikely", "no"]
    reason: str


class AlertItem(BaseModel):
    headline: str
    detail: str
    source_urls: list[str] = Field(min_length=1)


class WeeklyAlert(BaseModel):
    summary: str
    top_risks: list[AlertItem]
    dates_to_watch: list[AlertItem]
    suggested_actions: list[AlertItem]
