# Testing SmartGarage

Start with [developer setup](DEVELOPER_SETUP.md#automated-execution). Run the quick test from the repository root with the project virtual environment active.

```text
python scripts/quick_test.py --source TEST_REPORTS/raw/http/cancelled-output.mp4
```

Use `--config` with your own calibrated YAML, or `--preview-only` to explicitly skip database integration. The sample clip is a short fixture and does not establish a parking lifecycle. The quick test checks dependencies, preview outputs, Vehicle Intelligence health and Swagger, and the full pytest suite. With a local development `DATABASE_URL`, it also provisions a disposable PostgreSQL database and exercises Parking & Bay Management health, Swagger, authenticated delivery, acknowledgements, duplicate handling, slot release and direct database read-back. The native PostgreSQL service and an authorized development role must already be available.

`quick-test.json` records each result and links to its local log. Exit code 0 means all requested checks passed. Exit code 1 means a check failed. Exit code 2 means a prerequisite blocked a check. The PostgreSQL integration uses controlled events. The regression suite uses a temporary SQLite URL where tests need isolated database configuration. SQLite is never used as the application database in full mode.

For real footage acceptance, use `python scripts/run.py --mode full --config <development-yaml>` with a local PostgreSQL database and calibrated footage that establishes and closes its own parking session. Inspect both `outbox-verification.json` and `database-verification.json`. Restored history or a no-publisher preview cannot prove delivery. Cross-camera acceptance requires the same independently identified vehicle in multiple camera views.
