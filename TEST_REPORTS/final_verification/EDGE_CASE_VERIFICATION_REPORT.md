# Edge-case verification

The [edge-case matrix](EDGE_CASE_TEST_MATRIX.csv) records **32 PASS, 0 FAIL, 1 BLOCKED and 2 NOT TESTED**. It is a selected view of behaviors across the fault suite, existing regression cases and the HTTP harness.

| Area | Expected and observed |
| --- | --- |
| Input and detection | Invalid frames failed closed before inference. Missing and corrupt media failed at capture startup. Valid empty detections reached the tracker. |
| Tracking and attributes | Low scores did not seed tracks. Two tracks expired and reacquired distinct IDs after a capture gap. Backwards timestamps could not roll tracker time back. Colour estimation failure preserved the track and reported no colour. |
| Plates and lifecycle | Missing and weak evidence preserved unconfirmed ENTRY. Competing OCR candidates did not confirm. Asynchronous results remained keyed to their original vehicle. Slot ambiguity produced no arbitrary assignment, and unavailable occupancy was reported as UNKNOWN. |
| Interruptions | Premature decode failure, explicit cancellation and an isolated camera fault preserved evidence of partial progress. Live MJPEG terminated on job stop, and the finalized partial MP4 remained decodable. |
| Persistence and recovery | Injected write or serialization failure preserved the previous report. A SQLite insert failure rolled back. Retrying the same ID persisted and sent once. |
| API concurrency | A second start conflicted while processing continued. Validation failures and storage obstruction left the service healthy for later requests. |

The unverified edge rows cover semantic duplicates with fresh IDs and independent per-camera cancellation, which the job-level API does not expose. Remote database or Supabase failure was blocked by lack of an isolated target. See the [broader fault matrix](ERROR_HANDLING_TEST_MATRIX.csv) for more limits.
