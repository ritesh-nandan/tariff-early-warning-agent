"""SQLite storage. One file: data/tariffs.db."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,              -- federal_register | seed
    query TEXT,
    title TEXT NOT NULL,
    doc_type TEXT,
    subtype TEXT,
    agencies TEXT,
    publication_date TEXT,
    signing_date TEXT,
    effective_on TEXT,
    comments_close_on TEXT,
    html_url TEXT NOT NULL,
    raw_text_url TEXT,
    text TEXT,
    text_hash TEXT,
    truncated INTEGER DEFAULT 0,
    fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS threat_cards (
    doc_id TEXT PRIMARY KEY REFERENCES documents(doc_id),
    extractor TEXT NOT NULL,           -- llm | heuristic
    model TEXT,
    card_json TEXT NOT NULL,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS hts_rates (
    hts8 TEXT PRIMARY KEY,
    description TEXT,
    general_raw TEXT,
    general_pct REAL,                  -- NULL when the rate is not purely ad valorem
    special TEXT,
    source_url TEXT,
    fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS kalshi_markets (
    ticker TEXT PRIMARY KEY,
    series TEXT,
    title TEXT,
    yes_price REAL,                    -- 0-1 implied probability, NULL if no quote
    close_time TEXT,
    url TEXT,
    fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS llm_cache (
    key TEXT PRIMARY KEY,
    task TEXT,
    doc_id TEXT,
    model TEXT,
    response TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS source_status (
    source TEXT PRIMARY KEY,
    ok INTEGER,
    detail TEXT,
    checked_at TEXT
);
CREATE TABLE IF NOT EXISTS results (
    key TEXT PRIMARY KEY,
    json TEXT,
    updated_at TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path=None) -> sqlite3.Connection:
    config.ensure_dirs()
    con = sqlite3.connect(path or config.DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def upsert(con: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    con.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})", list(row.values()))


def set_result(con: sqlite3.Connection, key: str, value: Any) -> None:
    upsert(con, "results", {"key": key, "json": json.dumps(value), "updated_at": now()})
    con.commit()


def get_result(con: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = con.execute("SELECT json FROM results WHERE key=?", (key,)).fetchone()
    return json.loads(row["json"]) if row else default


def set_source_status(con: sqlite3.Connection, source: str, ok: bool, detail: str) -> None:
    upsert(con, "source_status", {"source": source, "ok": int(ok), "detail": detail, "checked_at": now()})
    con.commit()
