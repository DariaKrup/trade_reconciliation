"""Where the trades and confirmations come from.

A reader has a `name` (for messages) and `rows(after)`: the rows after the first `after` ones, as
(row number, {column: text}), exactly like a line of the CSV file. The bot remembers the last row number it
processed and asks for the rows after it on every poll. So rows must only be added at the end, in
inserted_at order, and never changed: then a row number always means the same row.
"""
from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from matching import TIME_FORMAT


class SourceError(Exception):
    """The source cannot be read right now (e.g. the database is down); the bot tries again at the next poll."""


class CsvReader:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.name = self.path.name

    def rows(self, after: int):
        if not self.path.exists():
            return
        with open(self.path, newline="") as fh:
            for number, row in enumerate(csv.DictReader(fh), 1):
                if number > after:
                    yield number, row


class PostgresReader:
    """Reads a table with the same columns as the CSV file, in inserted_at order.

    Needs the psycopg package:  pip install "psycopg[binary]"
    """

    def __init__(self, dsn: str, table: str, id_column: str, connect=None):
        if connect is None:
            import psycopg  # only needed when reading from PostgreSQL
            connect = psycopg.connect
        self.dsn, self.table, self.id_column, self.connect = dsn, table, id_column, connect
        self.name = f"{table} table"

    def rows(self, after: int):
        query = f"SELECT * FROM {self.table} ORDER BY inserted_at, {self.id_column} OFFSET %s"
        try:
            with self.connect(self.dsn) as conn:
                cursor = conn.execute(query, (after,))
                columns = [column[0] for column in cursor.description]
                fetched = cursor.fetchall()
        except Exception as exc:
            raise SourceError(f"cannot read {self.name}: {exc}") from exc
        for number, values in enumerate(fetched, after + 1):
            yield number, {column: _as_text(value) for column, value in zip(columns, values)}


def _as_text(value) -> str:
    """A PostgreSQL value as the text the CSV file would have: the bot parses both the same way."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime(TIME_FORMAT)
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)
