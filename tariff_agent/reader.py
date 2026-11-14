"""Step 2 - notice reader: one Federal Register notice -> one validated threat card."""
from __future__ import annotations

import json

from . import config, db, heuristics, llm
from .schemas import ThreatCard

PROMPT_VERSION = "reader-v1"

SYSTEM = """You read US Federal Register tariff notices for a small importer and extract a "threat card".

Return one JSON object and nothing else, with exactly these fields:
{
  "authority": "232" | "301" | "122" | "other",
  "status": "investigation" | "comments open" | "hearing" | "action announced" | "in force" | "struck down",
  "affected_categories": [short product-category phrases taken from the notice],
  "hts_codes": [HTS chapters, headings or subheadings that the notice itself lists as covered],
  "countries": [country names affected, or ["ALL"] when the action applies to all origins],
  "applies_to_all_products": true | false,
  "rate_low_pct": number | null,
  "rate_high_pct": number | null,
  "key_dates": {"initiated": date|null, "comment_deadline": date|null, "hearing": date|null,
                "statutory_deadline": date|null, "effective_date": date|null},
  "other_dates": [{"label": "short label", "date": date}],
  "likelihood_90d": number between 0 and 1,
  "rationale": "one sentence",
  "source_url": "the URL given in the metadata"
}

Rules:
- Use only what this notice says. Never invent or infer dates, rates or HTS codes; use null
  (or an empty list) whenever the notice does not state the fact. Do not use outside knowledge.
- Dates are YYYY-MM-DD. Rates are additional ad valorem duty percentages (25 means 25%).
  rate_low_pct and rate_high_pct span the range of additional duty rates the notice proposes
  or imposes; set both to the same value for a single rate.
- hts_codes: codes for covered products only. Exclude chapter 98/99 provision numbers. If the
  notice lists more than 60 codes, give the 4-digit headings instead.
- status describes the measure as of the notice's publication date. "action announced" means a
  duty has been decided or formally proposed but is not yet in effect; "in force" means it is
  already being collected; "struck down" means ended, revoked or invalidated. If a document
  concludes an investigation without imposing duties (for example by ordering negotiations),
  use "investigation", leave the rates null and say so in the rationale.
- key_dates.effective_date is the earliest date duties apply; put later staged dates in other_dates
  with a label starting "Effective". key_dates.statutory_deadline is a legal deadline or expiry
  stated in the notice.
- applies_to_all_products is true only for across-the-board measures (all or nearly all goods
  of the named countries), false for product-specific measures.
- likelihood_90d is your probability that a new or higher duty from this measure takes effect
  within 90 days after the publication date. Use 1.0 if it is already in force, 0.0 if struck
  down, and base rates otherwise: freshly opened investigations rarely conclude within 90 days.
- rationale: one plain sentence explaining the likelihood."""


def _user_prompt(doc: dict) -> str:
    text = doc["text"] or ""
    note = ""
    if len(text) > config.MAX_DOC_CHARS:
        note = (f"\nNOTE: the notice is {len(text):,} characters; only the first "
                f"{config.MAX_DOC_CHARS:,} are included below (annexes may be cut off).")
        text = text[: config.MAX_DOC_CHARS]
    meta = {k: doc.get(k) for k in ("doc_id", "title", "doc_type", "subtype", "agencies", "publication_date",
                                    "signing_date", "effective_on", "comments_close_on")}
    meta["source_url"] = doc["html_url"]
    return f"METADATA:\n{json.dumps(meta, indent=1)}{note}\n\nNOTICE TEXT:\n{text}"


def read_document(con, doc: dict, use_llm: bool, log=print) -> tuple[ThreatCard, str]:
    """Return (card, extractor). Falls back to the rule-based reader if the LLM fails twice."""
    if use_llm:
        try:
            card = llm.call_json("threat_card", doc["doc_id"], SYSTEM, _user_prompt(doc), ThreatCard, con,
                                 prompt_version=PROMPT_VERSION)
            card.source_url = doc["html_url"]  # the URL always comes from the database, not the model
            if doc["truncated"]:
                llm.log_call(task="threat_card", doc_id=doc["doc_id"], note="input truncated",
                             chars_sent=config.MAX_DOC_CHARS, chars_total=len(doc["text"] or ""))
            return card, "llm"
        except llm.LLMOutputError as exc:
            log(f"  ! LLM output invalid for {doc['doc_id']} after retry; using rule-based reader ({exc})")
        except Exception as exc:  # API/network failure: keep the pipeline alive
            log(f"  ! LLM call failed for {doc['doc_id']} ({type(exc).__name__}: {exc}); using rule-based reader")
    card = heuristics.read_notice(doc)
    llm.log_call(task="threat_card", doc_id=doc["doc_id"], model="offline-heuristic", cached=False,
                 input_tokens=0, output_tokens=0)
    return card, "heuristic"


def run(con, log=print) -> dict:
    use_llm = config.llm_available()
    log(f"Step 2 - reading notices ({'LLM: ' + config.MODEL if use_llm else 'OFFLINE rule-based reader: no API key found'})")
    docs = [dict(r) for r in con.execute("SELECT * FROM documents ORDER BY publication_date DESC")]
    have = {r["doc_id"]: r["extractor"] for r in con.execute("SELECT doc_id, extractor FROM threat_cards")}
    counts = {"llm": 0, "heuristic": 0, "skipped": 0}
    llm_budget = config.MAX_LLM_DOCS or len(docs)
    for doc in docs:
        prior = have.get(doc["doc_id"])
        # Keep existing cards, but upgrade rule-based cards once an API key is available.
        if prior == "llm" or (prior == "heuristic" and not use_llm):
            counts["skipped"] += 1
            continue
        go_llm = use_llm and llm_budget > 0
        card, extractor = read_document(con, doc, go_llm, log)
        if go_llm:
            llm_budget -= 1
        db.upsert(con, "threat_cards", {
            "doc_id": doc["doc_id"], "extractor": extractor,
            "model": config.MODEL if extractor == "llm" else None,
            "card_json": card.model_dump_json(), "created_at": db.now(),
        })
        con.commit()
        counts[extractor] += 1
    log(f"  threat cards: {counts['llm']} by LLM, {counts['heuristic']} rule-based, {counts['skipped']} already done")
    return {"use_llm": use_llm, **counts}
