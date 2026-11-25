"""Send the ENTRY/EXIT payloads from a finished run's report to Service 2.

    python tools/send_report_to_s2.py smart_parking_final_report.json            # dry run (default)
    python tools/send_report_to_s2.py smart_parking_final_report.json --send     # really POST

Configure --url or S2_URL and S2_API_KEY before sending.
Success requires an explicit persistence acknowledgement.
Events are sent in time order: ENTRY before its EXIT.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vehicle_intelligence.publishing.outbox import open_event_endpoint, post_event


def load_events(path):
    rep = json.load(open(path, encoding="utf-8"))
    events = rep.get("service2_events") or []
    order = {"ENTRY": 0, "EXIT": 1}
    return sorted(events, key=lambda e: (str(e.get("departure_at") or e.get("captured_at") or e.get("timestamp") or ""),
                                         order.get(e.get("event_type"), 9)))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--url", default=os.getenv("S2_URL"))
    ap.add_argument("--send", action="store_true", help="actually POST (default is a dry run)")
    a = ap.parse_args(argv)
    if a.send:
        if not a.url or not os.getenv("S2_API_KEY"):
            ap.error("Sending requires --url or S2_URL and S2_API_KEY.")
        try:
            opener = open_event_endpoint(a.url)
        except ValueError as exc:
            ap.error(str(exc))
    events = load_events(a.report)
    if not events:
        print("No service2_events in the report - nothing to send.")
        return 0
    print(f"{len(events)} event(s) [{'SEND' if a.send else 'DRY RUN'}]")
    bad = 0
    for e in events:
        tag = f"{e.get('event_type')} slot={(e.get('parking') or {}).get('slot_id')} id={e.get('event_id')}"
        if not a.send:
            print("  would send:", tag)
            continue
        ack, error, _ = post_event(a.url, json.dumps(e), 30, opener)
        if ack is None:
            bad += 1
            print(f"  FAIL {tag} ({error})")
        else:
            print(f"  {ack['status']} {tag} stored_event_id={ack['event_id']}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
