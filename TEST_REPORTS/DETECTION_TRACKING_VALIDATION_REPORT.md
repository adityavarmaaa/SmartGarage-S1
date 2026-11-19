# Detection and tracking validation

CPU, bundled YOLO weights, configured inference settings, and independent ByteTracker instances. Camera-to-output association was discovered from configured input stems and historical annotated output names. Technically usable footage was selected without substituting a camera-specific constant. The probe used 24 time-ordered samples from each excerpt, with two deliberately empty tracker inputs per camera. Actual detector output was recorded separately from these injected empty observations.

| Historical input | Sample frames | Detections total / peak per frame | Confidence range | Observed IDs → IDs seen after interruption | Annotated probe |
| --- | --- | --- | --- | --- | --- |
| `data/IMG_2138_entry_annotated.mp4` | 120–166 | 48 / 3 | 0.160–0.697 | [1, 2] → [1, 2] | [tracking-probe-1.mp4](raw/video/tracking-probe-1.mp4) |
| `data/IMG_2136_entry_annotated.mp4` | 34–80 | 30 / 2 | 0.163–0.900 | [1] → [1] | [tracking-probe-2.mp4](raw/video/tracking-probe-2.mp4) |

The reviewed sample frames show boxes aligned with visible vehicle silhouettes. One frame in the first excerpt had three detections and two confirmed tracks, which is recorded as a detection/confirmation difference. Existing overlays and low-detail background prevent adjudication of false or missed detections. No duplicate active tracker IDs were observed in the sampled outputs. Track absences and returns are recorded per sample, and identity continuity is limited to this short replay. The earlier tracking-loss fixture independently demonstrated expiration and new IDs on re-acquisition.

All outputs use the historical rendered input, which may affect detector behavior. This scope did not establish occlusion and re-entry continuity on raw footage, precision, recall, mAP, or MOT metrics. The status matrix records six passes and two blocked items, including those unavailable originals and real cross-camera truth. Both clips were regenerated without full-frame pixelation. All 48 output frames decoded at 540 × 1130, six frames per second. Historical overlays remain visible in the source. See the [quality audit](ANNOTATED_VIDEO_QUALITY_AUDIT.md).

See the [tracking matrix](DETECTION_TRACKING_VALIDATION_MATRIX.csv), [inventory](VIDEO_INVENTORY.csv), [per-frame results](raw/video/tracking-probes.json), and [evidence index](DETECTION_TRACKING_EVIDENCE_INDEX.md).
