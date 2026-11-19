# API and Swagger final audit

The mapper changed, so the existing loopback harness reran against the exact handover code using the bundled CPU detector. It passed **44 HTTP cases** and **10 documentation checks**. The generated schema is semantically identical to the included OpenAPI snapshot. Endpoint methods, models, examples, streaming types and error contracts are unchanged. The API version remains 1.2.0.

The [endpoint inventory and request/response audit](final_verification/API_AND_SWAGGER_VERIFICATION_REPORT.md) describe the retained routes. This sprint separately verified both status and liveness, valid and malformed uploads, missing/invalid values, synchronous storage failures, background configuration and capture failures, conflicting starts, cancellation while streaming, MJPEG, HEAD and finalized byte ranges. The invalid route and method checks returned the previously documented status codes. Live MJPEG ended after cancellation and returned a playable partial MP4. The Uvicorn log records application startup and shutdown. Chromium rendered Swagger and ReDoc.

S1 still runs one job per process and has no built-in authentication or job-specific lookup. The standalone still-frame route returns 501. No endpoint was added. See the [current HTTP matrix](API_TEST_RESULTS.csv) and [API evidence index](API_EVIDENCE_INDEX.md).

The HTTP and browser harness was rerun after incorporating the video correction. The checked-in schema and generated schema matched, and the Uvicorn trace recorded application shutdown. No deployed camera or production endpoint was used.
