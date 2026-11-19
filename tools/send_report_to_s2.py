"""Send the ENTRY/EXIT payloads from a finished run's report to Service 2.

    python tools/send_report_to_s2.py smart_parking_final_report.json            # dry run (default)
    python tools/send_report_to_s2.py smart_parking_final_report.json --send     # really POST

URL comes from --url, else $S2_URL, else the report's config default below.
Optional auth: set S2_API_KEY (header name: S2_API_KEY_HEADER, default X-API-Key).
HTTP 409 (already received) counts as success so re-sending is safe.
Events are sent in time order: ENTRY before its EXIT.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "https://smart-parking-data-service.onrender.com/internal/events/vehicle"


def load_events(path):
    rep = json.load(open(path, encoding="utf-8"))
    events = rep.get("service2_events") or []
    order = {"ENTRY": 0, "EXIT": 1}
    return sorted(events, key=lambda e: (str(e.get("captured_at") or e.get("timestamp") or ""),
                                         order.get(e.get("event_type"), 9)))


def post(url, event, timeout=30):
    headers = {"Content-Type": "application/json"}
    key = os.getenv("S2_API_KEY")
    if key:
        headers[os.getenv("S2_API_KEY_HEADER", "X-API-Key")] = key
    req = urllib.request.Request(url, data=json.dumps(event).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")[:300]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:  # network problem
        return None, repr(e)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--url", default=os.getenv("S2_URL", DEFAULT_URL))
    ap.add_argument("--send", action="store_true", help="actually POST (default is a dry run)")
    a = ap.parse_args(argv)
    events = load_events(a.report)
    if not events:
        print("No service2_events in the report - nothing to send.")
        return 0
    print(f"{len(events)} event(s) -> {a.url}  [{'SEND' if a.send else 'DRY RUN'}]")
    bad = 0
    for e in events:
        tag = f"{e.get('event_type'):<5} slot={((e.get('parking') or {}).get('slot_id'))} " \
              f"plate={((e.get('plate') or {}).get('text')) or '-'} ts={e.get('timestamp')}"
        if not a.send:
            print("  would send:", tag)
            continue
        status, body = post(a.url, e)
        ok = status is not None and (200 <= status < 300 or status == 409)
        bad += 0 if ok else 1
        print(f"  {'OK  ' if ok else 'FAIL'} HTTP {status}  {tag}\n        {body}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
