"""Run the whole Tariff Early-Warning pipeline:  python run.py

  1 collect notices, HTS rates and market data      5 landed-cost scenarios
  2 read each notice into a threat card (LLM)       6 weekly alert
  3 load the importer's products                    7 backtest
  4 match products to threats                       8 dashboard

Options:
  --importer FILE   analyse this CSV instead of data/sample_importer.csv
  --as-of DATE      treat YYYY-MM-DD as "today" (default: the real date)
  --offline         do not call the LLM even if an API key is configured
  --skip-collect    reuse what is already in data/tariffs.db
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

# Documents written to the manual seed file (used only if the Federal Register is down).
SEED_DOCS = [
    "2025-03439", "2025-04061", "2025-14893", "2025-10524", "2025-06587", "2026-06956", "2025-13345",
    "2026-16400", "2025-13365", "2026-16979", "2025-16191", "2026-22049", "2026-03824", "2026-03832",
    "2026-05151", "2026-11296", "2026-15181", "2026-05214", "2026-11314", "2026-13663", "2025-18729",
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tariff Early-Warning Agent")
    ap.add_argument("--importer", type=Path)
    ap.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--skip-collect", action="store_true")
    args = ap.parse_args(argv)
    if args.offline:
        os.environ["TARIFF_OFFLINE"] = "1"

    from tariff_agent import alert, backtest, collector, config, dashboard, db, matcher, reader, scoring, simulator

    config.ensure_dirs()
    con = db.connect()
    as_of: date = args.as_of
    use_llm = config.llm_available()
    print(f"Tariff Early-Warning Agent - as of {as_of.isoformat()} - model {config.MODEL if use_llm else 'none (offline)'}")

    # 1 - collect
    if args.skip_collect:
        print("Step 1 - skipped (--skip-collect)")
        used_seed = False
    else:
        summary = collector.run(con)
        used_seed = bool(summary.get("used_seed"))
        if summary["sources"].get("federal_register") and not config.SEED_PATH.exists():
            collector.write_seed(con, SEED_DOCS)
            print("  wrote data/seed_actions.json from live notices")
    if con.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"] == 0:
        print("No documents available (sources unreachable and no seed file). Stopping.")
        return 1

    # 2 - read notices
    reader.run(con)
    threats = scoring.load_threats(con)
    extractors = sorted({t.extractor for t in threats})

    # 3 - importer
    csv_path = args.importer or config.SAMPLE_IMPORTER
    print(f"Step 3 - importer file: {csv_path}")
    try:
        products = matcher.load_products(Path(csv_path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        print(f"  ! cannot read importer CSV: {exc}")
        return 1
    print(f"  {len(products)} products")

    # 4 + 5 - match and simulate
    print("Steps 4-5 - matching products to threats and simulating landed cost")
    rows = simulator.analyze(con, products, as_of, use_llm, threats)
    active = simulator.active_threats(threats, as_of)
    exposed = sum(r["costs"]["likely_delta_per_year"] > 0 for r in rows)
    print(f"  {len(active)} active measures; {exposed}/{len(rows)} products with a pending duty change; "
          f"likely +${sum(r['costs']['likely_delta_per_year'] for r in rows):,.0f}/yr, "
          f"worst +${sum(r['costs']['worst_delta_per_year'] for r in rows):,.0f}/yr")
    db.set_result(con, "products", rows)

    # 6 - alert
    print("Step 6 - weekly alert")
    timeline = scoring.upcoming_dates(active, as_of)
    brief, writer = alert.write_alert(con, rows, timeline, as_of, use_llm)
    (config.REPORTS / "weekly_alert.md").write_text(alert.to_markdown(brief, as_of, writer), encoding="utf-8")
    print(f"  reports/weekly_alert.md written by {writer}")
    db.set_result(con, "alert", brief.model_dump())

    # 7 - backtest
    bt = backtest.run(con, as_of, use_llm)
    (config.REPORTS / "backtest.md").write_text(backtest.to_markdown(bt), encoding="utf-8")
    db.set_result(con, "backtest", bt)

    # 8 - dashboard
    print("Step 8 - dashboard")
    reader_label = (f"Claude ({config.MODEL})" if extractors == ["llm"] else
                    "rule-based fallback (offline)" if extractors == ["heuristic"] else "mixed LLM / rule-based")
    payload = {
        "as_of": as_of.isoformat(),
        "counts": {"documents": con.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"], "active_threats": len(active)},
        "reader_label": reader_label,
        "offline": "heuristic" in extractors,
        "timeline": timeline,
        "products": rows,
        "products_label": f"Importer file: {Path(csv_path).name} ({len(rows)} products)",
        "threats": [simulator.threat_summary(t, as_of) for t in active],
        "alert": brief.model_dump(),
        "alert_writer": writer,
        "backtest": bt,
        "kalshi": [dict(r) for r in con.execute("SELECT title, yes_price, close_time, url FROM kalshi_markets ORDER BY series, ticker")],
        "sources": [dict(r) for r in con.execute("SELECT * FROM source_status ORDER BY source")],
        "used_seed": used_seed,
        "base_rates": {r["hts8"]: {"pct": r["general_pct"], "raw": r["general_raw"]} for r in con.execute("SELECT * FROM hts_rates")},
    }
    out = dashboard.write(payload)
    print(f"  dashboard written: {out}")
    print("Done. Open dashboard/index.html; see reports/backtest.md and reports/weekly_alert.md; LLM log in logs/llm_calls.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
