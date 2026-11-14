"""Step 5 - duty math. Plain, deterministic Python: the LLM never does arithmetic.

Simplifications (also listed in the README and write-up):
  * Duties are ad valorem on the customs value, taken here as the unit cost. Freight,
    insurance, the merchandise processing fee and harbor maintenance fee are excluded.
  * Stacking rule: within one legal authority only the highest applicable additional duty
    counts (overlapping Section 232 actions do not stack on each other); duties under
    different authorities add up, except that a Section 122 surcharge does not apply on
    top of a Section 232 duty (Proclamation of February 20, 2026, clause 4).
  * Section 232 "derivative" duties legally apply only to the metal content of a product.
    Content shares are not in the importer file, so the full value is used (an upper bound).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Duty:
    """One additional duty that applies, or may apply, to a product."""

    authority: str          # "232" | "301" | "122" | "other"
    low_pct: float          # bottom of the stated (or assumed) rate range
    high_pct: float         # top of the range
    probability: float      # chance it applies within 90 days; 1.0 when already in force
    in_force: bool
    label: str = ""

    def __post_init__(self):
        if self.low_pct < 0 or self.high_pct < self.low_pct:
            raise ValueError("rates must satisfy 0 <= low <= high")
        if not 0.0 <= self.probability <= 1.0:
            raise ValueError("probability must be between 0 and 1")

    @property
    def mid_pct(self) -> float:
        return (self.low_pct + self.high_pct) / 2.0


@dataclass(frozen=True)
class Scenario:
    base_pct: float
    today_pct: float        # base + additional duties in force now
    likely_pct: float       # probability-weighted
    worst_pct: float        # every matched duty lands at the top of its range


def combine_by_authority(rates: dict[str, float]) -> float:
    """Total additional duty from per-authority rates, applying the stacking rule."""
    r = {k: v for k, v in rates.items() if v > 0}
    if r.get("232", 0) > 0:
        r.pop("122", None)
    return sum(r.values())


def scenario_rates(base_pct: float, duties: list[Duty]) -> Scenario:
    """Today / likely / worst total duty rates (percent of customs value)."""
    if base_pct < 0:
        raise ValueError("base duty cannot be negative")
    authorities = {d.authority for d in duties}
    today: dict[str, float] = {}
    likely: dict[str, float] = {}
    worst: dict[str, float] = {}
    for a in authorities:
        mine = [d for d in duties if d.authority == a]
        current = max((d.low_pct for d in mine if d.in_force), default=0.0)
        today[a] = current
        # Expected increase over what is already paid, from the most damaging pending duty.
        bump = max((d.probability * max(0.0, d.mid_pct - current) for d in mine if not d.in_force), default=0.0)
        likely[a] = current + bump
        worst[a] = max((d.high_pct for d in mine), default=0.0)
    t = base_pct + combine_by_authority(today)
    return Scenario(
        base_pct=base_pct,
        today_pct=t,
        likely_pct=max(t, base_pct + combine_by_authority(likely)),
        worst_pct=max(t, base_pct + combine_by_authority(worst)),
    )


def duty_per_unit(unit_cost: float, rate_pct: float) -> float:
    return unit_cost * rate_pct / 100.0


def landed_cost_per_unit(unit_cost: float, rate_pct: float) -> float:
    """Unit cost plus duty (freight and fees excluded)."""
    return unit_cost + duty_per_unit(unit_cost, rate_pct)


def annual_cost(unit_cost: float, annual_units: float, rate_pct: float) -> float:
    return landed_cost_per_unit(unit_cost, rate_pct) * annual_units


def cost_table(unit_cost: float, annual_units: float, s: Scenario) -> dict:
    """Dollar view of a scenario: per unit, per year, and the change versus today."""
    out: dict = {"base_pct": s.base_pct}
    for name, rate in (("today", s.today_pct), ("likely", s.likely_pct), ("worst", s.worst_pct)):
        out[f"{name}_pct"] = round(rate, 4)
        out[f"{name}_per_unit"] = round(landed_cost_per_unit(unit_cost, rate), 4)
        out[f"{name}_per_year"] = round(annual_cost(unit_cost, annual_units, rate), 2)
    out["likely_delta_per_year"] = round(out["likely_per_year"] - out["today_per_year"], 2)
    out["worst_delta_per_year"] = round(out["worst_per_year"] - out["today_per_year"], 2)
    out["likely_delta_per_unit"] = round(out["likely_per_unit"] - out["today_per_unit"], 4)
    out["worst_delta_per_unit"] = round(out["worst_per_unit"] - out["today_per_unit"], 4)
    return out
