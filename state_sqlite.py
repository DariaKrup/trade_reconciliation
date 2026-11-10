"""What the bot remembers between polls and restarts, in one local SQLite file.

* cursor: how many rows of each source ('trade', 'confirmation') have been processed
* open_records: unconfirmed trades and unmatched confirmations, until their counterpart arrives

Changes are made inside `with state.step():`. Everything changed in
one step is saved together when the block ends, or (after an error, Ctrl+C or a crash) not at all.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cursor (
    source   TEXT PRIMARY KEY,
    position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS open_records (
    kind TEXT NOT NULL, id INTEGER NOT NULL,
    inserted_at TEXT NOT NULL, created_at TEXT NOT NULL, trade_date TEXT NOT NULL,
    instrument TEXT NOT NULL, price TEXT NOT NULL, volume TEXT NOT NULL, fee TEXT NOT NULL,
    currency TEXT NOT NULL, trader TEXT NOT NULL COLLATE NOCASE, broker TEXT NOT NULL COLLATE NOCASE,
    alerted INTEGER NOT NULL DEFAULT 0,       -- 1 once the 2-hour message has been sent
    PRIMARY KEY (kind, id)
);
CREATE INDEX IF NOT EXISTS open_by_deadline ON open_records (alerted, inserted_at);
"""
RECORD_COLUMNS = ("kind", "id", "inserted_at", "created_at", "trade_date", "instrument", "price", "volume",
                  "fee", "currency", "trader", "broker")

# The open record on the other side that agrees on the most fields (8 = confirmed, 7 = one field differs).
# Ties go to the closest execution time, then the oldest record.
FIND_COUNTERPART = """
SELECT *,
       (instrument = :instrument) + (price = :price) + (volume = :volume) + (fee = :fee)
     + (currency = :currency) + (trader = :trader) + (broker = :broker) + (trade_date = :trade_date) AS same
FROM open_records
WHERE kind != :kind AND same >= 7
ORDER BY same DESC, abs(julianday(created_at) - julianday(:created_at)), inserted_at, id
LIMIT 1
"""


class State:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)  # transactions are opened explicitly
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # -- writing (inside step() only) ----------------------------------------------------------------
    @contextmanager
    def step(self):
        """One step of the bot: the changes made in the block are saved together, or not at all."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:  # an error, or Ctrl+C in the middle: leave the state as it was
            self.db.execute("ROLLBACK")
            raise

    def set_position(self, source: str, value: int) -> None:
        self.db.execute("INSERT INTO cursor VALUES (?, ?) "
                        "ON CONFLICT (source) DO UPDATE SET position = excluded.position", (source, value))

    def add_open(self, rec: dict) -> None:
        self.db.execute(f"INSERT INTO open_records ({', '.join(RECORD_COLUMNS)}) "
                        f"VALUES ({', '.join('?' * len(RECORD_COLUMNS))})", [rec[c] for c in RECORD_COLUMNS])

    def remove_open(self, rec: dict) -> None:
        self.db.execute("DELETE FROM open_records WHERE kind = ? AND id = ?", (rec["kind"], rec["id"]))

    def mark_alerted(self, rec: dict) -> None:
        self.db.execute("UPDATE open_records SET alerted = 1 WHERE kind = ? AND id = ?", (rec["kind"], rec["id"]))

    # -- reading --------------------------------------------------------------------------------------
    def position(self, source: str) -> int:
        row = self.db.execute("SELECT position FROM cursor WHERE source = ?", (source,)).fetchone()
        return row[0] if row else 0

    def is_open(self, rec: dict) -> bool:
        return self.db.execute("SELECT 1 FROM open_records WHERE kind = ? AND id = ?",
                               (rec["kind"], rec["id"])).fetchone() is not None

    def find_counterpart(self, rec: dict) -> dict | None:
        """The open record on the other side that agrees with `rec` on all fields, or on all but one."""
        row = self.db.execute(FIND_COUNTERPART, rec).fetchone()
        return dict(row) if row else None

    def not_alerted_before(self, inserted_before: str) -> list[dict]:
        """Open records without a 2-hour message that appeared before `inserted_before`, oldest first."""
        rows = self.db.execute("SELECT * FROM open_records WHERE alerted = 0 AND inserted_at < ? "
                               "ORDER BY inserted_at, kind != 'trade', id", (inserted_before,))
        return [dict(r) for r in rows]

    def count_open(self, kind: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM open_records WHERE kind = ?", (kind,)).fetchone()[0]
