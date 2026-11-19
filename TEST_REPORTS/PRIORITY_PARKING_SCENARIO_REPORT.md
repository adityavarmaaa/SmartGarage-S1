# Priority parking scenario verification

**18 pass, 0 fail, 2 blocked, 3 not tested.** These behavior rows group related observations and reuse executable fixtures. Details and the exact pytest nodes are in the [scenario matrix](PRIORITY_PARKING_SCENARIO_MATRIX.csv).

- **Initialization and entry.** Initial-scan ownership required repeated observations and was preserved across later contact loss. Failed startup did not create free observations. Controlled tripwire and dwell allowed park start, and unconfirmed plate evidence preserved ENTRY.
- **Multiple vehicles, unknown occupancy and full lot.** Fixtures covered separate tracker IDs, association after loss, adjacent bay competition and reserved slot refusal. A new arrival remained unassigned when all synthetic slots were occupied.
- **Exit, interrupted processing and ambiguity.** A UID-only parked session closed once. An unmatched exit was withheld. Cancellation preserved the report and playable output. Ambiguous slot and OCR candidates did not create unsupported assignment or confirmation.
- **Multicamera and concurrency.** One camera's invalid geometry did not stop the other. Known synthetic identity supported handoff suppression. Parallel exact park-start retries produced one mapped sender call. Async OCR retained its original job keys.

The real footage probes are separate and use short historical annotated excerpts. A complete raw relocation and site-calibrated entry/exit sequence, physical stage-specific interruptions and overlapping independent report writers were not tested. Real confirmed OCR, cross-camera ReID and RTSP acceptance were blocked.
