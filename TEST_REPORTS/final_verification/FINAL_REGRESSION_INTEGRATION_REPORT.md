# Final regression and integration report

## Environment and execution

Linux x86_64, Python 3.12.14, isolated release extraction and workspace virtual environment. Tests used synthetic files, temporary SQLite, loopback HTTP and the bundled CPU YOLO weights. See [package versions](raw/environment.json) and [exact commands](REPRODUCTION_COMMANDS.md).

| Suite | Baseline passes | Final total | Passed | Failed | Errors | Skipped | Final duration |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| S1 with coverage | 112 | 157 | 157 | 0 | 0 | 0 | 17.91 s |
| Bundled S2 | 28 | 28 | 28 | 0 | 0 | 0 | 1.78 s |
| Total | 140 | 185 | 185 | 0 | 0 | 0 | 19.69 s |

The 45 added fault cases account for the difference. Existing code tests were retained byte for byte. S1 coverage was **59%**, up from 58% in the preceding release. Integration cases under `tests/integration/` contributed **81 passes**, with zero failures or blocked tests in that collected set. The JUnit logs contain every collected node and duration.

## Integration results

- Synthetic detection, tracking and geometry fixtures exercised slot ambiguity, unknown occupancy, time regression and multi-camera isolation.
- The loopback worker processed a real synthetic video with the bundled CPU model, streamed JPEG frames, cancelled and left a playable partial MP4 with a matching cancellation report.
- The outbox validated durable local rollback and recovery and an authenticated loopback publish with generated credentials.
- The S2 suite validated local event lookup, exit handling, geometry and authentication with temporary SQLite. No test routed to a production endpoint.
- Swagger and ReDoc browser rendering and schema checks passed with screenshots retained.

## Failures and fixes

The initial targeted run exposed contract gaps and faulty test doubles, with 12 failures across 32 cases. The targeted suite passed after fixing the identified defects, clarifying the mocks and adding follow-up tests. See the [fault report](FINAL_ERROR_HANDLING_FAULT_INJECTION_REPORT.md), [initial run](raw/fault-before.log) and [final targeted run](raw/fault-final.log).

**Verdict** PASS for the collected suites and loopback integration scope. Dependency warnings for the Starlette test adapter and bundled S2 settings remain. Native inference deadline, real camera/RTSP sources, production services, Windows/GPU and provisioned ANPR/OSNet remain outside this validation.
