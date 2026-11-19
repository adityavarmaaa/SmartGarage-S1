# Current API evidence

- [HTTP response cases](raw/http/http-responses.json) and [matrix](API_TEST_RESULTS.csv) preserve observed headers, bodies, statuses and diagnostics.
- [Live OpenAPI](raw/http/openapi.json) and [schema checks](raw/http/swagger-checks.json) match the documented route contracts.
- [Swagger screenshot](raw/http/swagger.png), [ReDoc screenshot](raw/http/redoc.png), and [browser checks](raw/http/browser-result.json) demonstrate the rendered pages.
- [Uvicorn trace](raw/http/uvicorn.log), [client log](raw/http-harness.log) and [child process result](raw/http/server-process.json) show real HTTP execution and shutdown.
- [Live JPEG](raw/http/live-preview.jpg), [cancelled MP4](raw/http/cancelled-output.mp4), and [cancelled report](raw/http/cancelled-report.json) are synthetic test evidence.
