"""Paths, environment and tunable assumptions. No secrets live here."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOGS = ROOT / "logs"
REPORTS = ROOT / "reports"
DASHBOARD = ROOT / "dashboard"
DB_PATH = DATA / "tariffs.db"
SEED_PATH = DATA / "seed_actions.json"
SAMPLE_IMPORTER = DATA / "sample_importer.csv"
BACKTEST_EVENTS = DATA / "backtest_events.json"

load_dotenv(ROOT / ".env")

# Model is read from the environment so the code runs unchanged on any Claude model.
MODEL = os.getenv("CLAUDE_MODEL", "claude-fable-5-2")

# Collection window and politeness.
START_DATE = os.getenv("TARIFF_START_DATE", "2025-01-20")
USER_AGENT = "tariff-early-warning-prototype/0.1"
HTTP_TIMEOUT = 40

# Long proclamations carry HTS annexes. We send at most this many characters to the
# LLM; anything cut is recorded in documents.truncated and logged (never silent).
MAX_DOC_CHARS = int(os.getenv("TARIFF_MAX_DOC_CHARS", "80000"))
# Optional cap on how many notices are sent to the LLM in one run (0 = no cap).
MAX_LLM_DOCS = int(os.getenv("TARIFF_MAX_LLM_DOCS", "0"))

# Scenario assumptions used ONLY by the simulator when a pending notice states no rate.
# They are labelled "assumed" everywhere they are used. Section 122 is capped at 15%
# by statute (19 U.S.C. 2132); the 232/301 ranges reflect rates actually imposed in
# 2025-2026 actions collected in this database.
ASSUMED_RATE_RANGE_PCT = {
    "232": (25.0, 50.0),
    "301": (10.0, 25.0),
    "122": (10.0, 15.0),
    "other": (10.0, 25.0),
}

ADVISORY = (
    "Advisory only. This is an automated early-warning estimate, not legal advice and "
    "not customs-broker advice. Confirm classification, rates and dates with a licensed "
    "customs broker or trade attorney before acting."
)


def llm_available() -> bool:
    """True when Anthropic credentials exist and offline mode is not forced."""
    if os.getenv("TARIFF_OFFLINE") == "1":
        return False
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


def ensure_dirs() -> None:
    for p in (DATA, LOGS, REPORTS, DASHBOARD):
        p.mkdir(parents=True, exist_ok=True)
