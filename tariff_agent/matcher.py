"""Step 4 - exposure matcher: which threat cards touch which products, and why.

Deterministic rules decide clear cases (HTS overlap + country). Only ambiguous cases -
category-concordance matches and possible "derivative" products - go to the LLM, which
must return a verdict with a written reason. Without an API key those cases are kept at
a reduced strength and labelled unverified.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import asdict, dataclass

from . import concordance, llm
from .schemas import MatchJudgement
from .scoring import Threat

REQUIRED_COLUMNS = ["sku", "description", "hts_code", "country_of_origin", "unit_cost_usd", "annual_units"]

COUNTRY_ALIASES = {
    "prc": "china", "people's republic of china": "china", "cn": "china",
    "uk": "united kingdom", "great britain": "united kingdom", "gb": "united kingdom",
    "korea": "south korea", "republic of korea": "south korea", "kr": "south korea",
    "russian federation": "russia", "viet nam": "vietnam", "vn": "vietnam",
    "mx": "mexico", "ca": "canada", "de": "germany", "jp": "japan", "in": "india",
    "tw": "taiwan", "br": "brazil", "usa": "united states", "us": "united states",
}

STRENGTH_BY_VERDICT = {"yes": 0.9, "likely": 0.7, "unlikely": 0.3}
JUDGE_PROMPT_VERSION = "judge-v1"
JUDGE_SYSTEM = """You help a small US importer decide whether a tariff measure plausibly covers one of its products.

You are given a product (description, 10-digit HTS code, country of origin) and a summary of a tariff
notice. A deterministic rule flagged the pair as an ambiguous or "derivative" match. Judge whether the
product falls within the scope the notice describes, using only the information provided.

Return one JSON object and nothing else:
{"covered": "yes" | "likely" | "unlikely" | "no", "reason": "one or two plain sentences"}

Guidance: "derivative" products are goods made substantially from a covered material (for example
steel fasteners under a steel measure). Say "unlikely" or "no" when the product only shares a keyword
or a broad chapter with the measure. Do not invent HTS codes or rates."""


@dataclass
class Product:
    sku: str
    description: str
    hts_code: str            # digits only
    country_of_origin: str
    unit_cost_usd: float
    annual_units: float


@dataclass
class Match:
    doc_id: str
    method: str              # hts | country-wide | concordance | derivative
    strength: float          # 0-1 confidence that the measure covers the product
    reason: str
    llm_reviewed: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def norm_country(name: str) -> str:
    n = re.sub(r"\s+", " ", (name or "").strip().lower())
    return COUNTRY_ALIASES.get(n, n)


def norm_hts(code: str) -> str:
    return re.sub(r"\D", "", code or "")


def fmt_hts(digits: str) -> str:
    parts = [digits[:4], digits[4:6], digits[6:8], digits[8:10]]
    return ".".join(p for p in parts if p)


def load_products(csv_text: str) -> list[Product]:
    """Parse an importer CSV. Raises ValueError with a readable message on bad input."""
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("﻿")))
    cols = [c.strip().lower() for c in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED_COLUMNS if c not in cols]
    if missing:
        raise ValueError(f"CSV is missing column(s): {', '.join(missing)}. Required: {', '.join(REQUIRED_COLUMNS)}")
    out: list[Product] = []
    for n, raw in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        if not any(row.values()):
            continue
        hts = norm_hts(row["hts_code"])
        if len(hts) < 6:
            raise ValueError(f"line {n}: hts_code '{row['hts_code']}' needs at least 6 digits")
        try:
            cost = float(row["unit_cost_usd"].replace("$", "").replace(",", ""))
            units = float(row["annual_units"].replace(",", ""))
        except ValueError:
            raise ValueError(f"line {n}: unit_cost_usd and annual_units must be numbers") from None
        if cost < 0 or units < 0:
            raise ValueError(f"line {n}: unit_cost_usd and annual_units cannot be negative")
        out.append(Product(row["sku"] or f"row{n}", row["description"], hts, row["country_of_origin"], cost, units))
    if not out:
        raise ValueError("CSV has no product rows")
    return out


def country_applies(card_countries: list[str], product_country: str) -> bool:
    """True when the measure covers the product's origin (empty list = not stated = assume yes)."""
    if not card_countries:
        return True
    normed = {norm_country(c) for c in card_countries}
    return "all" in normed or norm_country(product_country) in normed


def hts_overlap(product_hts: str, card_codes: list[str]) -> str | None:
    """Longest notice code that is a prefix of the product's HTS code."""
    hits = [c for c in card_codes if c and product_hts.startswith(c)]
    return max(hits, key=len) if hits else None


def match_product(p: Product, t: Threat) -> Match | None:
    """Deterministic match of one product against one threat. None = no plausible link."""
    card = t.card
    if not country_applies(card.countries, p.country_of_origin):
        return None
    code = hts_overlap(p.hts_code, card.hts_codes)
    if code:
        strength = 1.0 if len(code) >= 6 else 0.8 if len(code) >= 4 else 0.6
        level = "subheading" if len(code) >= 6 else "heading" if len(code) >= 4 else "chapter"
        return Match(t.doc_id, "hts", strength,
                     f"HTS {level} {fmt_hts(code)} listed in the notice covers product code {fmt_hts(p.hts_code)}.")
    if card.applies_to_all_products and card.countries:
        scope = "all origins" if "all" in {norm_country(c) for c in card.countries} else p.country_of_origin
        return Match(t.doc_id, "country-wide", 0.9,
                     f"Across-the-board measure covering products of {scope}; exemptions in the notice's annexes not checked.")
    for topic in t.topics:
        prefix = hts_overlap(p.hts_code, concordance.hts_prefixes(topic))
        if prefix:
            if card.hts_codes:
                return Match(t.doc_id, "derivative", 0.4,
                             f"Product is in the '{topic}' family (HTS {fmt_hts(prefix)}), but the codes listed in the notice do not include it.")
            return Match(t.doc_id, "concordance", 0.7,
                         f"Notice names '{topic}' without HTS codes; product code {fmt_hts(p.hts_code)} falls under {fmt_hts(prefix)} in the category concordance.")
    for topic in t.topics:
        if concordance.product_mentions_topic(topic, p.description):
            return Match(t.doc_id, "derivative", 0.35,
                         f"Product description suggests a possible '{topic}' derivative, but its HTS code is outside the usual range.")
    return None


def judge_with_llm(con, p: Product, t: Threat, m: Match) -> Match | None:
    """Ask the LLM to confirm or reject an ambiguous match. Returns None when rejected."""
    card = t.card
    user = (
        f"PRODUCT\n- description: {p.description}\n- HTS code: {fmt_hts(p.hts_code)}\n- country of origin: {p.country_of_origin}\n\n"
        f"TARIFF NOTICE\n- title: {t.title}\n- authority: Section {card.authority}\n- status: {card.status}\n"
        f"- affected categories: {', '.join(card.affected_categories) or 'not stated'}\n"
        f"- HTS codes listed: {', '.join(fmt_hts(c) for c in card.hts_codes[:60]) or 'none listed'}\n"
        f"- countries: {', '.join(card.countries) or 'not stated'}\n\n"
        f"WHY IT WAS FLAGGED\n{m.reason}"
    )
    verdict = llm.call_json("match_judgement", f"{t.doc_id}:{p.sku}", JUDGE_SYSTEM, user, MatchJudgement, con,
                            prompt_version=JUDGE_PROMPT_VERSION)
    if verdict.covered == "no":
        return None
    return Match(t.doc_id, m.method, STRENGTH_BY_VERDICT[verdict.covered],
                 f"{verdict.reason} (LLM review: {verdict.covered}.)", llm_reviewed=True)


def match_all(p: Product, threats: list[Threat], con=None, use_llm: bool = False, log=print) -> list[tuple[Threat, Match]]:
    out: list[tuple[Threat, Match]] = []
    for t in threats:
        m = match_product(p, t)
        if m is None:
            continue
        if m.method in ("concordance", "derivative"):
            if use_llm and con is not None:
                try:
                    m = judge_with_llm(con, p, t, m)
                except Exception as exc:  # keep the rule-based match if the review fails
                    log(f"  ! match review failed for {p.sku}/{t.doc_id}: {type(exc).__name__}: {exc}")
                    m.reason += " (Unverified: LLM review failed.)"
                if m is None:
                    continue
            else:
                m.reason += " (Unverified: no LLM review in offline mode.)"
        out.append((t, m))
    return out
