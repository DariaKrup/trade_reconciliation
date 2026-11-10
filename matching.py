"""Matching rules: how a CSV row becomes a record that can be compared field by field.

The matching itself is one SQL query in state_sqlite.py: it counts how many of FIELDS are equal. Pure functions
here, no files or database.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
TRADE, CONF = "trade", "confirmation"
ID_COLUMN = {TRADE: "trade_id", CONF: "confirmation_id"}

# The fields a trade and its confirmation must agree on. All 8 equal: confirmed; 7: a mismatch.
FIELDS = ("instrument", "price", "volume", "fee", "currency", "trader", "broker", "trade_date")

# Reference data: brokers often send tickers where we book names.
INSTRUMENT_ALIASES = {"NVDA": "NVIDIA", "AAPL": "APPLE", "MSFT": "MICROSOFT", "AMZN": "AMAZON",
                      "TSLA": "TESLA", "NFLX": "NETFLIX", "META PLATFORMS": "META"}


def parse_time(text: str) -> datetime:
    return datetime.strptime(text.strip(), TIME_FORMAT)


def _number(col: str, text: str) -> str:
    """'129.450' -> '129.45', so equal numbers are equal strings."""
    try:
        return format(Decimal(text.strip()).normalize(), "f")
    except InvalidOperation:
        raise ValueError(f"{col} is not a number: {text!r}") from None


def _text(col: str, text: str | None) -> str:
    text = " ".join((text or "").split())
    if not text:
        raise ValueError(f"{col} is empty")
    return text


def parse_row(kind: str, row: dict) -> dict:
    """CSV row -> record with the open_records columns, normalised so that a plain `=` compares them.
    Raises ValueError (or KeyError for a missing column)."""
    rec = {"kind": kind, "id": int(row[ID_COLUMN[kind]])}
    for col in ("inserted_at", "created_at"):
        rec[col] = parse_time(row[col]).strftime(TIME_FORMAT)
    rec["trade_date"] = rec["created_at"][:10]
    for col in ("price", "volume", "fee"):
        rec[col] = _number(col, row[col])
    instrument = _text("instrument", row["instrument"]).upper()
    rec["instrument"] = INSTRUMENT_ALIASES.get(instrument, instrument)
    rec["currency"] = _text("currency", row["currency"]).upper()
    rec["trader"] = _text("trader", row["trader"])  # compared case-insensitively (COLLATE NOCASE)
    rec["broker"] = _text("broker", row["broker"])
    return rec


def differences(trade: dict, conf: dict) -> list[str]:
    """The fields that differ, to explain a mismatch in the message (names ignore case, as in the query)."""
    def value(rec, field):
        return rec[field].casefold() if field in ("trader", "broker") else rec[field]
    return [f for f in FIELDS if value(trade, f) != value(conf, f)]
