# Broker Trades Reconciliation

`reconcile.py` is a bot that runs continuously during the day. It treats the trades and the broker confirmations as append-only streams and tells Operations about problems as they happen. It reads them from the CSV files `internal_trades.csv` and `external_confirmations.csv`, or from PostgreSQL tables of the same names.

## Running it

Python 3.10+. Reading the CSV files needs no packages.

```bash
python3 reconcile.py            # runs until Ctrl+C
python3 reconcile.py --reset    # forget the state and messages, start again from the first row
```

Options: `--trades`, `--confirmations` (CSV paths), `--out` (default `output/`) and `--poll-seconds` (default 5).

### Reading from PostgreSQL

```bash
pip install "psycopg[binary]"
python3 reconcile.py --postgres postgresql://localhost/recon --out output_postgres
```

- `--postgres DSN` reads the tables `internal_trades` and `external_confirmations` of that database instead of the CSV files. They must have the same columns as the CSV files. The bot only reads them, in `inserted_at, id` order.
- `--out` works as with the CSV files. Use a separate folder (or `--reset`) when switching between the CSV files and PostgreSQL, because the saved positions belong to one source.

Everything else works the same way: the messages, the state (still a local SQLite file) and the crash safety. If the database cannot be reached, the bot prints a warning and tries again at the next poll. 


## What it does

Every few seconds (`--poll-seconds`) it reads the rows added to either source since the last check and processes them in `inserted_at` order. Each new trade or confirmation is looked up among the open records on the other side.

| Situation | Message |
|---|---|
| They agree on all fields | none: the trade is confirmed |
| They agree on all fields but one | `MISMATCH`, sent immediately |
| A trade still has no confirmation 2 hours after it appeared | `UNCONFIRMED` |
| A confirmation still has no trade 2 hours after it appeared | `UNMATCHED CONFIRMATION` |
| The counterpart arrives after that 2-hour message | `RESOLVED` if it matches, otherwise `MISMATCH` with a note |

Every message says what is wrong and why it might have happened.
Messages are appended to `output/messages.txt`; on start the bot prints that path to stdout.

## Matching rules

A trade and a confirmation are compared on 8 fields: instrument, price, volume, fee, currency, trader, broker and trade date (the day of `created_at`). There is no tolerance: all 8 must be equal for the trade to be confirmed. Please check the [Trade-offs and decisions section](#trade-offs-and-decisions). 


The lookup is a single query over the open records of the other side. It counts how many of the 8 fields are equal and takes the best one.


## State and crashes

All state is in one local SQLite file, `output/state.sqlite3`, and only `state_sqlite.py` touches it. It uses Python's built-in `sqlite3`, so there is nothing to install. It has two tables:

| Table | Contents |
|---|---|
| `cursor` | How many rows of each source have been processed (`trade` → 50, `confirmation` → 48). |
| `open_records` | Unconfirmed trades and unmatched confirmations, each with an `alerted` flag for whether its 2-hour message has gone out. Matched records are deleted. After the sample week it holds 3 trades and 1 confirmation. |

The 2-hour check is a query for open records with `alerted = 0` and `inserted_at` at least 2 hours ago.

The bot is safe to kill at any moment, and no message is ever lost:

- **One step per row.** Everything a row changes (2-hour messages sent, a record opened or matched, the row done) happens inside `with state.step():`, which saves it all in one SQLite transaction when the block ends. The bot itself never begins, commits or rolls back.
- **Messages are written as soon as they are produced**, inside the step and so before it is saved: appended to `messages.txt` and flushed to disk.
- **After a crash** before a step is saved, nothing of that row is saved, and it is processed again on the next start. Any of its messages already written appear a second time. Ctrl+C works the same way.

## Files

| File | Contents |
|---|---|
| `reconcile.py` | The bot: the polling loop, matching each new row, the 2-hour checks, writing the messages, and the command line. |
| `readers.py` | Where the rows come from: `CsvReader` and `PostgresReader`. Both return rows as text, the way the CSV file has them. |
| `state_sqlite.py` | Everything that touches SQLite: the open records, the positions and the matching query. They are changed only inside `with state.step():`. |
| `matching.py` | The field rules: parsing and normalising rows, listing the differing fields. Pure functions, no files or database. |
| `messages.py` | The texts for Operations: what is wrong and why it might have happened. |


## Trade-offs and decisions

### Matching rules
- Matching requires all fields to be equal; a pair that differs in exactly one field is reported as `MISMATCH`. In real life, there are a few approaches I'd consider:
  - Checking text fields for typos: e.g., with Levenshtein distance, and reporting them as potential matches for the manual approval;
  - Keeping the instrument aliases in reference data instead of a hard-coded table;
  - Introducing a tolerance on price or fee;
  - Considering more flexibility for trade date (e.g., for trades around midnight).

### Operations
- Processed (confirmed) trades are not sent or mentioned as the Operations team should probably be notified only on exceptions;
- Unmatched records stay in the state until their counterpart arrives, and nothing expires them; in production old ones would be archived after a set period;
- A repeated ID is reported and skipped only while the first record is still open; after a match it is treated as a new record;
- Each row describes one complete transaction: no splits or cancellations.

### Technical
- The state is kept in a local SQLite file, so nothing needs to be deployed; the cost is that only one bot process on one machine can use it;
- For the trades and for the confirmations, the bot remembers one number: how many rows it has processed so far, and on each check skips that many rows and processes the rest. This assumes rows are only ever added at the end, in `inserted_at` order, and never changed. With PostgreSQL this has two costs: skipping gets slower as the table grows (fine for a few hundred trades a day), and a row committed late with an earlier `inserted_at` shifts the count, so that row is missed and the last processed row is processed again;
- With a few hundred trades a day, re-reading the files (or querying past the saved position) on every check is cheap. For larger volumes the bot should read only the newly added rows instead of the whole file each time;
- Only one copy of the bot runs on an `output/` folder at a time.
