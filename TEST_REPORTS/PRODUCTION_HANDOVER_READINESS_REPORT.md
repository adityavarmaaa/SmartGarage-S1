# Production handover readiness assessment

**Ready for production engineering handover and isolated staging. Live deployment approval is conditional.** The package contains the actual runnable S1/S2 source, verified CPU path, operations and recovery procedures, test evidence and verified archive manifests.

## Release identity and scope

The source was a fresh extraction of the verified `SMART_PARKING_S1_FINAL_EVIDENCE.zip` at SHA-256 `93a54d65c19183ab51ded83bad60c320bb2990e633b124af89a3c357f5253883`. The code declares S1 package version 0.1.0 and API 1.2.0. This handover is identified by its manifest and archive checksum. The prior 185-test result and 59% coverage are independently traceable to preserved evidence. Historical video and plate records were inspected read-only in the original supplied project extraction.

## Current results

| Area | Result |
| --- | --- |
| Detection/tracking | Six passed, two blocked. Forty-eight CPU samples across two historical annotated clips. Both trackers re-observed IDs after injected interruptions. |
| B-03 | Inconclusive. Four candidate records, three UID aliases, no defensibly matched raw entry/exit or slot-linked event sequence. |
| Priority parking | 18 passed, two blocked, three not tested. |
| Lifecycle/event | 13 passed, one blocked, two not tested. Six reproduced defects fixed; exact retry semantics and conflict guards verified. |
| Fault handling | 45 targeted pytest cases reran; combined matrix 90 passed, four blocked and 15 not tested. |
| API/docs | 44 HTTP and 10 Swagger/OpenAPI/browser checks passed. |
| Regression | 200 passed; 81 S1 integration passes; zero failures, errors or skips. S1 coverage 61%. |
| Prior edge matrix | 32 passed, one blocked, two not tested. Current scenario suites extend that coverage. |

## Deployment gates and risks

Production needs accessible raw camera feeds, site-calibrated geometry, approved ANPR/ReID providers, a capacity acceptance run, agreed treatment of unconfirmed identity, secured API ingress and staging validation of publisher and storage credentials. Current verification used Linux CPU and isolated state. Physical cameras, live RTSP, real ANPR/OSNet, Windows, GPU, and remote persistence were outside scope. No external production events were sent.

The mapper now refuses conflicting identity or stale exits and preserves state for manual reconciliation. Receipt keys cover exact timestamp/camera/UID/slot retries from this release. Legacy receipts, identity changes across cameras, shifted timestamps, cloud acknowledgement recovery, native stalls and site retention need operator controls. The [operations guide](../docs/PRODUCTION_OPERATIONS_GUIDE.md) describes controlled startup, monitoring, shutdown and rollback. The [checklist](PRODUCTION_HANDOVER_CHECKLIST.csv) records remaining gates.

The unified archive contains the corrected evidence renderer and both regenerated full-detail probe clips. Two renderer regressions and complete decoding of the clips passed. Including this evidence-only issue, the handover fixes seven verified defects. The six lifecycle defects remain documented separately. See [video quality audit](ANNOTATED_VIDEO_QUALITY_AUDIT.md).
