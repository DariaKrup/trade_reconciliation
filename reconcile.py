#!/usr/bin/env python3
"""Broker trades reconciliation bot.

Runs continuously during the day and treats the trades and the confirmations as append-only streams.

The rows come from the CSV files or from PostgreSQL tables (readers.py). What the bot remembers is in a local
SQLite file (state_sqlite.py): the open items and how far each source has got. All changes caused by one row
are saved at once, so after a crash the bot continues where it stopped, without losing a message.

The field rules are in matching.py, the message texts in messages.py.
"""
from __future__ import annotations

import argparse
import heapq
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import messages
from matching import CONF, ID_COLUMN, TIME_FORMAT, TRADE, differences, parse_row, parse_time
from readers import CsvReader, PostgresReader, SourceError
from state_sqlite import State

HERE = Path(__file__).resolve().parent
SLA = timedelta(hours=2)
MESSAGES = "messages.txt"
TABLES = {TRADE: "internal_trades", CONF: "external_confirmations"}  # also the CSV file names


def due(rec: dict) -> datetime:
    """The moment an unmatched record must be reported."""
    return parse_time(rec["inserted_at"]) + SLA


class Bot:
    """Each input row is one step, `with self.state.step():`, whose changes to the state are saved together.
    Messages are written as soon as they are produced, before the step is saved. After a crash
    the unsaved row is processed again, so its messages may appear twice, but none is lost."""

    def __init__(self, trades, confirmations, state: State, out_dir: Path):
        self.readers = {TRADE: trades, CONF: confirmations}
        self.state = state
        self.messages_path = Path(out_dir) / MESSAGES
        self.messages_path.parent.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        self.state.close()

    # -- main loop --------------------------------------------------------------------------------
    def poll(self, now: datetime) -> None:
        """Process everything new in inserted_at order, then send the 2-hour messages that are due by `now`."""
        def stream(kind):
            last = ""  # a bad row keeps its place in the source order
            for number, row in self.readers[kind].rows(after=self.state.position(kind)):
                try:
                    rec, error = parse_row(kind, row), None
                    last = rec["inserted_at"]
                except (ValueError, KeyError, TypeError) as exc:
                    rec, error = None, str(exc)
                yield (last, kind == CONF, number), rec, kind, number, error

        # Interleave the two sources by inserted_at (a trade first on a tie), as if rows arrived one by one.
        for _, rec, kind, number, error in heapq.merge(stream(TRADE), stream(CONF)):
            with self.state.step():
                if rec is None:
                    self.report(now, messages.bad_row(self.readers[kind].name, number, error))
                else:
                    self.report_overdue(parse_time(rec["inserted_at"]))
                    self.on_record(rec, number)
                self.state.set_position(kind, number)
        with self.state.step():
            self.report_overdue(now)

    def on_record(self, rec: dict, number: int) -> None:
        now = parse_time(rec["inserted_at"])
        if self.state.is_open(rec):
            self.report(now, messages.received_twice(self.readers[rec["kind"]].name, number, rec))
            return
        other = self.state.find_counterpart(rec)
        if other is None:  # nothing to pair with yet: wait for the counterpart
            self.state.add_open(rec)
            return
        self.state.remove_open(other)
        trade, conf = (rec, other) if rec["kind"] == TRADE else (other, rec)
        update = ""
        if other["alerted"]:  # the 2-hour message already went out: tell Ops what happened since
            update = messages.late_update(other, rec, due(other), now - parse_time(other["inserted_at"]))
        issues = differences(trade, conf)
        if issues:
            self.report(now, messages.mismatch(trade, conf, issues, update))
        elif update:
            self.report(now, messages.resolved(trade, conf, update))
        # all fields equal and on time: confirmed, nothing to report

    def report_overdue(self, until: datetime) -> None:
        """Report every open item whose 2 hours ran out before `until`."""
        for rec in self.state.not_alerted_before((until - SLA).strftime(TIME_FORMAT)):  # deadline = inserted_at + 2h
            self.state.mark_alerted(rec)
            msg = messages.unconfirmed(rec) if rec["kind"] == TRADE else messages.unmatched_confirmation(rec)
            self.report(due(rec), msg)

    # -- output -----------------------------------------------------------------------------------
    def report(self, at: datetime, msg: str) -> None:
        """Append a message to messages.txt right away (before the step is saved)."""
        with open(self.messages_path, "a") as fh:
            fh.write(f"{at:%Y-%m-%d %H:%M:%S}  {msg}\n\n")
            fh.flush()
            os.fsync(fh.fileno())  # on disk before the step is saved, so a power cut cannot lose it


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Reconcile internal trades against broker confirmations, continuously.")
    p.add_argument("--trades", type=Path, default=HERE / f"{TABLES[TRADE]}.csv")
    p.add_argument("--confirmations", type=Path, default=HERE / f"{TABLES[CONF]}.csv")
    p.add_argument("--postgres", metavar="DSN",
                   help=f"read the {TABLES[TRADE]} and {TABLES[CONF]} tables of this PostgreSQL database "
                        "instead of the CSV files, e.g. postgresql://localhost/recon")
    p.add_argument("--out", type=Path, default=HERE / "output", help="where messages.txt and state.sqlite3 go")
    p.add_argument("--poll-seconds", type=float, default=5.0, help="how often to look for new rows")
    p.add_argument("--reset", action="store_true", help="forget the state and messages, start from the first row")
    args = p.parse_args(argv)

    if args.postgres:
        try:
            trades, confirmations = (PostgresReader(args.postgres, TABLES[kind], ID_COLUMN[kind])
                                     for kind in (TRADE, CONF))
        except ImportError:
            print('error: reading from PostgreSQL needs the psycopg package: pip install "psycopg[binary]"',
                  file=sys.stderr)
            return 1
    else:
        trades, confirmations = CsvReader(args.trades), CsvReader(args.confirmations)
    db_path = args.out / "state.sqlite3"
    if args.reset:
        for name in ("state.sqlite3", "state.sqlite3-journal", MESSAGES):
            (args.out / name).unlink(missing_ok=True)
    bot = Bot(trades, confirmations, State(db_path), args.out)

    print(f"Watching {trades.name} and {confirmations.name} every {args.poll_seconds:g}s. Ctrl+C to stop.\n",
          file=sys.stderr)
    print(f"Messages for Operations are written to {bot.messages_path}", flush=True)
    unreachable = False
    try:
        while True:
            try:
                bot.poll(datetime.now())
                unreachable = False
            except SourceError as exc:  # e.g. the database is down: nothing is lost, try again at the next poll
                if not unreachable:  # warn once per outage, not on every poll
                    print(f"warning: {str(exc).splitlines()[0]}; trying again every {args.poll_seconds:g}s",
                          file=sys.stderr)
                unreachable = True
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:  # safe at any moment, like a crash: an unfinished row is processed again next time
        pass
    print(f"\nStopped. Open: {bot.state.count_open(TRADE)} trade(s), {bot.state.count_open(CONF)} confirmation(s), "
          f"saved in {db_path}", file=sys.stderr)
    bot.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
