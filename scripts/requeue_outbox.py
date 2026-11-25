"""Inspect or requeue one retained failed event after reconciliation."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("outbox", type=Path)
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--apply", action="store_true", help="Clear the retry state for the selected failed row")
    args = parser.parse_args()
    mode = "rw" if args.apply else "ro"
    try:
        with closing(sqlite3.connect(f"{args.outbox.resolve().as_uri()}?mode={mode}", uri=True)) as db, db:
            if args.apply:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT attempts, last_error FROM outbox WHERE event_id=? AND sent=0 "
                "AND acknowledgement IS NULL AND failed_at IS NOT NULL",
                (args.event_id,),
            ).fetchone()
            if row is None:
                parser.error("No retained failed event matches this ID.")
            if args.apply:
                db.execute(
                    "UPDATE outbox SET attempts=0, failed_at=NULL, next_try=0, last_error=NULL WHERE event_id=?",
                    (args.event_id,),
                )
    except sqlite3.Error:
        parser.error("Cannot open a compatible outbox. Check the existing file and delivery schema.")
    print(json.dumps({"event_id": args.event_id, "previous_attempts": row[0],
                      "previous_error": row[1], "requeued": args.apply}))


if __name__ == "__main__":
    main()
