# Recorded-video acceptance

Use the [full-mode setup](DEVELOPER_SETUP.md#full-system) with disposable PostgreSQL and an authorized, calibrated recording. A controlled database smoke tests delivery and storage separately.

## Capture one complete parking lifecycle

1. Mount a fixed camera with an unobstructed view of the configured tripwire and the chosen bay. Keep its original angle, orientation, crop and resolution. If the camera is moved, recalibrate with the existing geometry tools before using it for acceptance.
2. Start recording while the bay is free and before the vehicle approaches. Capture the vehicle crossing the entry line in the configured direction and moving fully into the bay. Confirm the geometry covers the bay and the applicable entry and departure path.
3. Keep the same vehicle in view long enough for the configured dwell and occupancy confirmation. Then record it physically leaving and crossing the exit line. Continue until its former bay is visibly free long enough for the configured release confirmation.
4. Use continuous original footage. Keep exposure and focus adequate for the vehicle and plate where permitted. Record duration, original orientation and frame rate, the camera-to-geometry mapping, and timestamps or replay anchor policy. Do not cut across a transition, combine different recordings into one timeline, or use a recording that starts with the target vehicle already parked.
5. For cross-camera acceptance, capture both calibrated views with overlapping coverage or a known handoff path. Keep their clocks aligned and record an operator-verified reference for the same vehicle. Include similar vehicles if possible to expose false merges. Do not publish full plates or credential material in an acceptance report.

Supply one development YAML with its own camera IDs, local sources, geometry and parking areas. Disable historical exit restoration for connected acceptance. Explicitly request full mode and leave publishing enabled in the development configuration. Run from the repository root:

```text
python scripts/run.py --mode full --config <calibrated-recording-yaml>
```

Exit 0 is a verified connected parking lifecycle for at least one camera-local identity. The runner correlates observed gate ENTRY, bay PARK_START, bay PARK_END and gate EXIT with the acknowledged database event IDs and the closed session. The verified bay must be free in JSON and available in PostgreSQL. Restored or inferred observations cannot satisfy the sequence. When processing and database checks work but the recording supplies insufficient lifecycle evidence, the runner exits 2 with `status: blocked`. A processing or persistence failure exits 1.

Review `report.json`, `annotated.mp4`, `outbox-verification.json`, `database-verification.json`, `footage-suitability.json` and `execution-summary.json`. The suitability report identifies the observed events and exact correlated row IDs without plate values. It does not verify cross-camera identity. Review both original views and the identity report separately for that acceptance.

See [database schema and contract](DATABASE.md). The existing mapping stores a PostgreSQL ENTRY for PARK_START and a PostgreSQL EXIT for PARK_END. Gate crossings remain in the Vehicle Intelligence evidence. If the application later needs each gate and bay observation stored independently in PostgreSQL, an additive design could persist source events keyed by their existing sender IDs and link them to mapped events and sessions. Existing consumers and historical rows would retain their meanings. This requires separate compatibility and migration validation.
