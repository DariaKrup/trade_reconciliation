"""Messages for Operations: what is wrong and why it might have happened.

Each function returns the text of a message; reconcile.py puts the time in front of it.
"""
from __future__ import annotations

from datetime import datetime, timedelta

LABELS = {"instrument": "Instrument", "price": "Price", "volume": "Volume", "fee": "Fee",
          "currency": "Currency", "trader": "Trader", "broker": "Broker", "trade_date": "Trade date"}

WHY = {
    "instrument": "The wrong instrument was picked at booking or on the confirmation.",
    "price": "The price was keyed wrongly, or the broker confirmed a different (e.g. average) fill price.",
    "volume": "The quantity was keyed wrongly, or the order was only partly filled.",
    "fee": "{broker} charged a different commission than we booked: fee schedule change or manual entry error.",
    "currency": "One side recorded the wrong settlement currency.",
    "trader": "The trade is booked under a different trader than the broker reports.",
    "broker": "The trade is booked with a different broker than the one that confirmed it.",
    "trade_date": "The trade was booked on a different day than the broker executed it, or a cut-off issue.",
}


def label(rec: dict) -> str:
    return f"{rec['kind']} {rec['id']}"


def terms(rec: dict) -> str:
    return (f"{rec['instrument']} {rec['volume']} @ {rec['price']} {rec['currency']}, fee {rec['fee']}, "
            f"{rec['trader']} / {rec['broker']}, traded {rec['created_at']}")


def fmt_gap(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _message(kind, headline, what=(), why=(), notes=()) -> str:
    """The message text, without the time that reconcile.py puts in front of it."""
    lines = [f"{kind} {headline}"]
    for title, texts in (("What", what), ("Why ", why), ("Note", notes)):
        lines += [f"{'':21}{title if i == 0 else '    '} | {text}" for i, text in enumerate(texts)]
    return "\n".join(lines)


# -- the counterpart arrived after the 2-hour message ----------------------------------------------
def late_update(waiting: dict, arrived: dict, alerted_at: datetime, waited: timedelta) -> str:
    return (f"Update: {label(waiting)} was reported as unmatched at {alerted_at:%Y-%m-%d %H:%M}; "
            f"{label(arrived)} arrived {fmt_gap(waited)} after it.")


def resolved(trade: dict, conf: dict, update: str) -> str:
    return _message("RESOLVED", f"trade {trade['id']} is now confirmed by confirmation {conf['id']}",
                    what=[f"Both: {terms(trade)}"], notes=[update])


# -- a trade and a confirmation differ in one field ------------------------------------------------
def mismatch(trade: dict, conf: dict, fields: list[str], update: str = "") -> str:
    what = [f"Internal: {terms(trade)}", f"Broker:   {terms(conf)}"]
    what += [f"{LABELS[f]}: internal '{trade[f]}' vs broker '{conf[f]}'" for f in fields]
    why = [WHY[f].format(broker=conf["broker"]) for f in fields]
    return _message("MISMATCH", f"trade {trade['id']} vs confirmation {conf['id']}: {', '.join(fields)}",
                    what=what, why=why, notes=[update] if update else [])


# -- nothing matched within 2 hours ----------------------------------------------------------------
def unconfirmed(trade: dict) -> str:
    broker = trade["broker"]
    return _message(
        "UNCONFIRMED", f"trade {trade['id']}: no confirmation from {broker} 2 hours after booking",
        what=[f"Internal: {terms(trade)}, booked {trade['inserted_at']}"],
        why=[f"Possibly: {broker} has not confirmed it yet, our booking differs from the confirmation "
             "in more than one field, or the trade was booked twice or by mistake."])


def unmatched_confirmation(conf: dict) -> str:
    broker = conf["broker"]
    return _message(
        "UNMATCHED CONFIRMATION", f"confirmation {conf['id']}: no internal trade 2 hours after {broker} sent it",
        what=[f"Broker: {terms(conf)}, received {conf['inserted_at']}"],
        why=["Possibly: we have not booked the trade yet, our booking differs from the confirmation in more "
             f"than one field, or {broker} sent it by mistake."])


# -- input problems --------------------------------------------------------------------------------
def bad_row(file_name: str, number: int, error: str) -> str:
    return _message("BAD ROW", f"{file_name} row {number} skipped: {error}")


def received_twice(file_name: str, number: int, rec: dict) -> str:
    return _message("BAD ROW", f"{file_name} row {number}: {label(rec)} received twice; ignored")
