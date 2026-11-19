# Final error handling and fault-injection verification

## Result and scope

The dedicated fault suite passed **45 pytest cases**. The [fault matrix](ERROR_HANDLING_TEST_MATRIX.csv) combines those cases, relevant unchanged regressions and selected loopback HTTP observations. It contains **83 PASS, 0 FAIL, 4 BLOCKED and 15 NOT TESTED**. Matrix rows describe behaviors and can reference shared test cases. Their count is independent of the pytest count.

Tested the detector boundary, tracker, async OCR, attribute fallback, camera geometry, parking lifecycle, capture and output, camera isolation, atomic report replacement, SQLite outbox and HTTP error handling. The full project inventory is retained. The runtime follows capture → detector → tracker → parking bridge → lifecycle → publishing/reporting. The API uses a single background job, an upload directory and the multicamera video recorder.

## Method and evidence

Used an independent extraction of the verified release in a Linux Python 3.12 virtual environment. Fixtures generated identifiers and frames, loaded project calibration for the real HTTP run, and used temporary output paths. Faults included injected exceptions, malformed arrays, SQLite trigger failure, filesystem error codes and controlled configuration failures. Only loopback HTTP and generated test credentials were used for publishing verification.

- [Initial fault execution](raw/fault-before.log) recorded 20 passes and 12 failures. The initial tracker and capture doubles also exposed fixture defects. Corrected the temporary doubles to retain real tracker state and numeric capture metrics while preserving the injected failure and assertions.
- [Final targeted run](raw/fault-final.log) contains raw diagnostics, tracebacks and pytest outcomes. [JUnit](raw/fault-final.xml) carries case identifiers and timing.
- [Final S1 and S2 raw results](FINAL_REGRESSION_INTEGRATION_REPORT.md) verify interactions with existing behavior.
- [HTTP responses](raw/http-final/http-responses.json) include request IDs, status, error disclosures and job states.

## Verified defects and fixes

| Defect | Root cause and fix | Evidence |
| --- | --- | --- |
| F-01 Invalid frames counted as successful | Bridge received unvalidated payloads. Validate decoded pixels and metadata before inference and tracking. | D01–D04 |
| F-02 Missing or duplicate tracker IDs | The tracker result was consumed before identifier checks. Reject the affected association result before downstream updates. | T01, T02 |
| F-03 Invalid calibration dimensions | Geometry validation checked points and slots but omitted frame size. Require two positive dimensions when size is specified and preserve the legacy unspecified-size sentinel. | G01–G04 |
| F-04 One malformed camera frame interrupted independent work | Scheduler read image shape before validating the first frame. Isolate invalid startup frames and reject unexpected runtime dimensions before they reach inference. | M04, M09 |
| F-05 Report persistence failure appeared successful | The multicamera runner only propagated its final report exception on cancellation. Propagate persistence failure on ordinary completion and retain an existing processing exception when one is already active. | R03, R04 |
| F-06 Backwards capture timestamp could affect tracker state | The bridge accepted timestamp regression. Drop the backwards frame and keep the last accepted clock. | T05 |
| F-07 All-camera startup failure lost its category | Startup raised generic exceptions after per-camera diagnostics. Raise typed terminal diagnostics with the camera scope. | M12, H29 |

## Recovery and remaining limits

The tested pipeline continued after invalid detector output, an association failure, an OCR worker failure and isolated camera faults. Unconfirmed reads left the plate null without ending the entry lifecycle. Available cameras retained independent processing. Report and outbox failures surfaced without claiming successful persistence. Exact event-ID deduplication was verified. Semantic repeats under fresh IDs and several negative lifecycle transitions still lack dedicated tests.

**WARNING** A camera-specific slot exception was isolated and logged by the scheduler with a traceback through its generic frame-processing boundary. That path has less structured context than the dedicated detector and tracker boundaries. Native inference calls cannot be preempted by the tested cooperative cancellation mechanism. The full list of unverified targets and reasons is in the matrix.
