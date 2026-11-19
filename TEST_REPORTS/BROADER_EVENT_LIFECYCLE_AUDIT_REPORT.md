# Broader event and lifecycle audit

**13 pass, 0 fail, 1 blocked, 2 not tested.** The [case matrix](EVENT_LIFECYCLE_AUDIT_MATRIX.csv), [synthetic JSON trace](raw/lifecycle-trace.json), and [targeted log](raw/target-after.log) show source times, mapped S2 records, UID, slot, and plate state.

The raw sequence was ENTRY → PARK_START → PARK_END → EXIT. S2 receives mapped ENTRY and EXIT from PARK_START and PARK_END. A raw gate EXIT is internal. The synthetic trace preserved the UID, slot, first observation and event time, with unconfirmed plate status. An unmatched end was withheld. Cases applied in reverse report order still produced one time-ordered report cycle.

| Finding | Severity | Change and verification |
| --- | --- | --- |
| Fresh-ID replay of PARK_START can queue a second mapped ENTRY | High | Durable observation receipt scoped to camera, area, UID, slot and exact source timestamp. Retry preserves the first event ID. Restart and concurrent retry tests passed. |
| Replayed gate ENTRY can reopen PARKED state | High | Ignore the same entry time and older comparable entry times for the matching UID session. |
| Conflicting plate and UID/slot could close a different session | High | Reject conflicting association before enqueue or close. Both synthetic sessions remained parked. |
| Duplicate raw observations split report cycles | Medium | Collapse exact repeated stage/time/slot for the same camera and UID. A duplicate test passed for each raw stage. |
| Non-confirmed plate text could reach the mapped payload | High | Clear candidate plate fields while preserving UID and plate status. Controlled candidate did not reach the sender. |
| Older PARK_END could close a newer cycle | High | Reject an end whose source time precedes the active parked observation. |

The final targeted cases executed against the previous verified source produced [11 failures and 2 passes](raw/verified-source-before.log). On the handover source all 13 passed. The SQLite receipt means accepted by the local sender/outbox. A crash after local queueing and before the receipt is marked can retry with the same outbox event ID. Receipts only apply to observations written by this release, and depend on stable identifiers and exact source-time representation. A shifted timestamp, changed UID, independent camera alias, and arbitrary replay ordering still need operational reconciliation. Older databases gain the additive table; historical rows are not backfilled.

Cross-camera suppression was demonstrated with controlled handoff identity input. A real identity model, cloud destination acknowledgement/recovery and a complete real-life exit sequence remain unverified.
