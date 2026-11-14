"""Step 1 - data collector.

Sources (all free and public):
  * Federal Register API  - tariff notices and proclamations (Sections 232 / 301 / 122)
  * USITC HTS REST API    - base (column 1 general) duty rates and descriptions
  * Kalshi public API     - tariff-related prediction markets (optional context only)

Every source is probed before use. If the Federal Register is unreachable the collector
falls back to data/seed_actions.json, a small labelled seed built from real notices.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
from typing import Any

import requests

from . import config, db

FR_BASE = "https://www.federalregister.gov/api/v1/documents.json"
HTS_SEARCH = "https://hts.usitc.gov/reststop/search"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

FR_FIELDS = [
    "title", "document_number", "publication_date", "type", "subtype", "abstract",
    "html_url", "raw_text_url", "agencies", "signing_date", "effective_on",
    "comments_close_on",
]

# (label, search term, extra filter). "section 122" alone matches unrelated statutes
# (CERCLA, Medicare), so it is only searched within presidential and USTR documents.
FR_QUERIES: list[tuple[str, str, dict[str, str]]] = [
    ("pres_232", '"section 232"', {"conditions[type][]": "PRESDOCU"}),
    ("pres_301", '"section 301"', {"conditions[type][]": "PRESDOCU"}),
    ("pres_122", '"section 122"', {"conditions[type][]": "PRESDOCU"}),
    ("bis_232", '"section 232"', {"conditions[agencies][]": "industry-and-security-bureau"}),
    ("ustr_301", '"section 301"', {"conditions[agencies][]": "trade-representative-office-of-united-states"}),
    ("ustr_122", '"section 122"', {"conditions[agencies][]": "trade-representative-office-of-united-states"}),
]

# Titles that match the search terms but are not tariff actions.
IRRELEVANT_TITLE = re.compile(
    r"Agency Information Collection|Foreign-Trade Zone|Firearms|Sunshine Act|Privacy Act",
    re.I,
)

PRESIDENTIAL_TARIFF_TITLE = re.compile(
    r"tariff|dut(?:y|ies)|imports? of|import surcharge|section 301|section 232|trade (?:deal|agreement)|"
    r"united states-japan agreement|economic prosperity deal|threats to the united states",
    re.I,
)

SESSION = requests.Session()
SESSION.headers["User-Agent"] = config.USER_AGENT


def _get(url: str, params: dict | None = None) -> requests.Response:
    r = SESSION.get(url, params=params, timeout=config.HTTP_TIMEOUT)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------- reachability

def check_sources(con) -> dict[str, bool]:
    """Probe each endpoint once and record the outcome in source_status."""
    probes = {
        "federal_register": (FR_BASE, {"per_page": 1, "conditions[term]": '"section 232"'}),
        "usitc_hts": (HTS_SEARCH, {"keyword": "7318.15.20"}),
        "kalshi": (f"{KALSHI_BASE}/events", {"limit": 1}),
    }
    status: dict[str, bool] = {}
    for name, (url, params) in probes.items():
        try:
            r = _get(url, params)
            r.json()
            status[name] = True
            db.set_source_status(con, name, True, f"HTTP {r.status_code}")
        except Exception as exc:  # network errors, bad JSON, HTTP errors
            status[name] = False
            db.set_source_status(con, name, False, f"{type(exc).__name__}: {exc}"[:300])
    return status


# ------------------------------------------------------------ Federal Register

def clean_fr_text(raw: str) -> str:
    """The full-text endpoint returns the notice wrapped in minimal HTML."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text)
    return text.strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fr_search(term: str, extra: dict[str, str]) -> list[dict[str, Any]]:
    params: list[tuple[str, str]] = [
        ("per_page", "100"),
        ("order", "newest"),
        ("conditions[term]", term),
        ("conditions[publication_date][gte]", config.START_DATE),
    ]
    params += list(extra.items())
    params += [("fields[]", f) for f in FR_FIELDS]
    out: list[dict[str, Any]] = []
    url: str | None = FR_BASE
    first = True
    while url:
        data = _get(url, params if first else None).json()
        out += data.get("results", [])
        url = data.get("next_page_url")
        first = False
    return out


def collect_federal_register(con, log=print) -> int:
    """Fetch metadata and full text for tariff notices. Already-stored docs are skipped."""
    known = {r["doc_id"] for r in con.execute("SELECT doc_id FROM documents WHERE text IS NOT NULL")}
    seen: dict[str, tuple[str, dict]] = {}
    for label, term, extra in FR_QUERIES:
        results = _fr_search(term, extra)
        kept = 0
        for d in results:
            title = d.get("title") or ""
            if IRRELEVANT_TITLE.search(title):
                continue
            # Many unrelated executive orders cite these statutes in passing.
            if label.startswith("pres_") and not PRESIDENTIAL_TARIFF_TITLE.search(title):
                continue
            seen.setdefault(d["document_number"], (label, d))
            kept += 1
        log(f"  federal register [{label}]: {len(results)} found, {kept} relevant")

    new = 0
    for doc_id, (label, d) in seen.items():
        if doc_id in known:
            continue
        text = ""
        if d.get("raw_text_url"):
            try:
                text = clean_fr_text(_get(d["raw_text_url"]).text)
                time.sleep(0.15)  # be polite
            except Exception as exc:
                log(f"  ! full text unavailable for {doc_id}: {exc}")
        if not text:
            text = (d.get("abstract") or d["title"]).strip()
        db.upsert(con, "documents", {
            "doc_id": doc_id,
            "source": "federal_register",
            "query": label,
            "title": d["title"],
            "doc_type": d.get("type"),
            "subtype": d.get("subtype"),
            "agencies": "; ".join(a.get("name") or a.get("raw_name") or "" for a in d.get("agencies") or []),
            "publication_date": d.get("publication_date"),
            "signing_date": d.get("signing_date"),
            "effective_on": d.get("effective_on"),
            "comments_close_on": d.get("comments_close_on"),
            "html_url": d["html_url"],
            "raw_text_url": d.get("raw_text_url"),
            "text": text,
            "text_hash": text_hash(text),
            "truncated": int(len(text) > config.MAX_DOC_CHARS),
            "fetched_at": db.now(),
        })
        new += 1
    con.commit()
    return new


def load_seed(con, log=print) -> int:
    """Fallback: load the manual seed file (real notices, each with its source URL)."""
    if not config.SEED_PATH.exists():
        log("  ! seed file missing")
        return 0
    seed = json.loads(config.SEED_PATH.read_text(encoding="utf-8"))
    n = 0
    for d in seed["documents"]:
        if con.execute("SELECT 1 FROM documents WHERE doc_id=?", (d["doc_id"],)).fetchone():
            continue
        text = d.get("text") or d["title"]
        db.upsert(con, "documents", {
            "doc_id": d["doc_id"], "source": "seed", "query": "seed", "title": d["title"],
            "doc_type": d.get("doc_type"), "subtype": d.get("subtype"), "agencies": d.get("agencies"),
            "publication_date": d.get("publication_date"), "signing_date": d.get("signing_date"),
            "effective_on": d.get("effective_on"), "comments_close_on": d.get("comments_close_on"),
            "html_url": d["html_url"], "raw_text_url": d.get("raw_text_url"), "text": text,
            "text_hash": text_hash(text), "truncated": 0, "fetched_at": db.now(),
        })
        n += 1
    con.commit()
    return n


def write_seed(con, doc_ids: list[str], excerpt_chars: int = 6000) -> None:
    """Regenerate the seed file from real notices already in the database."""
    docs = []
    for doc_id in doc_ids:
        r = con.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
        if not r:
            continue
        docs.append({
            "doc_id": r["doc_id"], "title": r["title"], "doc_type": r["doc_type"],
            "subtype": r["subtype"], "agencies": r["agencies"],
            "publication_date": r["publication_date"], "signing_date": r["signing_date"],
            "effective_on": r["effective_on"], "comments_close_on": r["comments_close_on"],
            "html_url": r["html_url"], "raw_text_url": r["raw_text_url"],
            "text": (r["text"] or "")[:excerpt_chars],
        })
    payload = {
        "_label": (
            "MANUAL SEED - fallback used only when the Federal Register API is unreachable. "
            "Each entry is a real Federal Register notice (metadata plus the opening excerpt "
            "of its text) with its source URL in html_url."
        ),
        "generated_from": "federalregister.gov API",
        "documents": docs,
    }
    config.SEED_PATH.write_text(json.dumps(payload, indent=1), encoding="utf-8")


# ------------------------------------------------------------------- USITC HTS

def fmt_hts(code: str, digits: int) -> str:
    """'7318152030' -> '7318.15.20' (digits=8) or '7318.15.20.30' (digits=10)."""
    c = re.sub(r"\D", "", code)[:digits]
    parts = [c[:4], c[4:6], c[6:8], c[8:10]]
    return ".".join(p for p in parts if p)


def parse_ad_valorem(raw: str | None) -> float | None:
    """'Free' -> 0.0, '3.5%' -> 3.5. Specific or compound rates return None."""
    if raw is None:
        return None
    s = raw.strip()
    if s.lower() == "free":
        return 0.0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%", s)
    return float(m.group(1)) if m else None


def fetch_hts_rate(con, code: str, log=print) -> dict | None:
    """Look up the column-1 general duty for an HTS code (cached at 8 digits)."""
    digits = re.sub(r"\D", "", code)
    hts8 = digits[:8]
    row = con.execute("SELECT * FROM hts_rates WHERE hts8=?", (hts8,)).fetchone()
    if row:
        return dict(row)
    key8 = fmt_hts(digits, 8)
    try:
        items = _get(HTS_SEARCH, {"keyword": key8}).json()
    except Exception as exc:
        log(f"  ! HTS lookup failed for {key8}: {exc}")
        return None
    by_no = {i.get("htsno"): i for i in items if i.get("htsno")}
    rate_item = None
    for cand in (fmt_hts(digits, 10), key8):
        it = by_no.get(cand)
        if it and (it.get("general") or "").strip():
            rate_item = it
            break
    if rate_item is None:
        log(f"  ! no general rate found for HTS {key8}")
        return None
    desc_item = by_no.get(fmt_hts(digits, 10)) or rate_item
    rec = {
        "hts8": hts8,
        "description": (desc_item.get("description") or rate_item.get("description") or "").strip(),
        "general_raw": rate_item["general"].strip(),
        "general_pct": parse_ad_valorem(rate_item["general"]),
        "special": (rate_item.get("special") or "").strip(),
        "source_url": f"https://hts.usitc.gov/search?query={key8}",
        "fetched_at": db.now(),
    }
    db.upsert(con, "hts_rates", rec)
    con.commit()
    time.sleep(0.1)
    return rec


# ---------------------------------------------------------------------- Kalshi

KALSHI_TITLE = re.compile(r"tariff|section 232|section 301", re.I)


def collect_kalshi(con, log=print, max_series: int = 45) -> int:
    """Store open tariff-related markets. Context only: never used in the duty math."""
    series = _get(f"{KALSHI_BASE}/series", {"limit": 1000}).json().get("series", [])
    picked = [s for s in series if KALSHI_TITLE.search(s.get("title") or "")][:max_series]
    con.execute("DELETE FROM kalshi_markets")
    n = 0
    for s in picked:
        try:
            markets = _get(f"{KALSHI_BASE}/markets", {"series_ticker": s["ticker"], "status": "open", "limit": 20}).json().get("markets", [])
        except Exception:
            continue
        for m in markets:
            price = None
            for f in ("last_price_dollars", "yes_bid_dollars"):
                try:
                    v = float(m.get(f))
                    if v > 0:
                        price = v
                        break
                except (TypeError, ValueError):
                    pass
            if price is None and m.get("last_price"):
                price = float(m["last_price"]) / 100.0
            title = m.get("title") or s["title"]
            sub = m.get("yes_sub_title") or m.get("subtitle") or ""
            db.upsert(con, "kalshi_markets", {
                "ticker": m["ticker"], "series": s["ticker"],
                "title": f"{title} - {sub}" if sub and sub not in title else title,
                "yes_price": price, "close_time": m.get("close_time"),
                "url": f"https://kalshi.com/markets/{s['ticker'].lower()}",
                "fetched_at": db.now(),
            })
            n += 1
        time.sleep(0.1)
    con.commit()
    return n


# ------------------------------------------------------------------------ main

def run(con, log=print) -> dict[str, Any]:
    log("Step 1 - collecting data")
    status = check_sources(con)
    for name, ok in status.items():
        log(f"  source {name}: {'reachable' if ok else 'UNREACHABLE'}")
    summary: dict[str, Any] = {"sources": status}
    if status["federal_register"]:
        try:
            summary["new_documents"] = collect_federal_register(con, log)
        except Exception as exc:
            log(f"  ! Federal Register collection failed ({exc}); using seed file")
            status["federal_register"] = False
    if not status["federal_register"]:
        log("  Federal Register unreachable -> loading labelled manual seed (data/seed_actions.json)")
        summary["new_documents"] = load_seed(con, log)
        summary["used_seed"] = True
    if status["kalshi"]:
        try:
            summary["kalshi_markets"] = collect_kalshi(con, log)
        except Exception as exc:
            log(f"  ! Kalshi collection failed: {exc}")
    total = con.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
    summary["total_documents"] = total
    log(f"  documents in DB: {total} ({summary.get('new_documents', 0)} new); "
        f"kalshi markets: {summary.get('kalshi_markets', 0)}")
    return summary
