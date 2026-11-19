# Edge-case verification

The [inherited edge-case report](final_verification/EDGE_CASE_VERIFICATION_REPORT.md) and [matrix](final_verification/EDGE_CASE_TEST_MATRIX.csv) preserve 32 passed, one blocked, two not tested. The underlying executable suites reran as part of the current full S1 run. The [priority matrix](PRIORITY_PARKING_SCENARIO_MATRIX.csv) and [event audit matrix](EVENT_LIFECYCLE_AUDIT_MATRIX.csv) provide newer evidence for full parking, association conflicts and delayed or repeated observations.

A high-detail synthetic frame remained identical outside its overlays. An odd-size BGR image was padded for encoding while retaining every source pixel. The resulting MP4 decoded at the padded dimensions with the expected channel values. See [video quality audit](ANNOTATED_VIDEO_QUALITY_AUDIT.md) and [targeted JUnit](raw/unified-targeted.xml). These two checks supplement the inherited matrix count.
