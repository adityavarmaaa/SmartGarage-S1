# Handover release notes

Baseline S1 package 0.1.0 and API 1.2.0 declarations are unchanged. The handover manifest identifies this evidence release.

- Added durable exact-observation PARK_START receipts, guarded late ENTRY and stale/ambiguous exit processing, sanitized non-confirmed plate fields and removed exact duplicate lifecycle report rows.
- Added 13 production-handover pytest cases covering retries, restart persistence, concurrent input, full lot, rejected conflicts, time ordering and mapped lifecycle identity.
- Added annotated CPU probe clips, video inventory, B-03 redacted audit, updated fault/API matrices, operator guides and reproducible local preview.
- 200 regressions passed with 61% S1 coverage. Current Uvicorn and rendered documentation checks passed.
- Historical raw sources and protected crop material remain in the authorized source archive where available. The package holds redacted metadata and pixelated probe evidence. Full real deployment validation remains conditional.

The unified archive contains the corrected evidence renderer and both regenerated full-detail probe clips. Two renderer regressions and complete decoding of the clips passed. Including this evidence-only issue, the handover fixes seven verified defects. The six lifecycle defects remain documented separately. See [video quality audit](ANNOTATED_VIDEO_QUALITY_AUDIT.md).

The submission revision adds the requested CSV names and independent rerun evidence. Project source, configuration and tests match the preserved verified ZIP byte for byte.
