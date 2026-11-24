# AI agent setup

1. Locate the repository root and check Git status before changing files. Read the [README](../README.md), [architecture](ENGINEERING_HANDOVER/01_SYSTEM_ARCHITECTURE.md), and [developer setup](DEVELOPER_SETUP.md).
2. Check the Python version, available PostgreSQL instance, and writable development output directory. Confirm the database and any destination are isolated from production.
3. Ask for missing credentials, recordings, calibrated geometry, or model assets. Keep real secrets in the process environment. Use the documented local preview when site assets are unavailable.
4. Use a separate YAML and the documented setup command. Run the initializer's read-only check. The setup can create the database where the login has permission and provision known camera geometry. It cannot create site calibration, camera credentials, or missing models.
5. Start Parking Management and check its database health and OpenAPI document. Start Vehicle Intelligence in preview mode and check its liveness and API.
6. Run the isolated PostgreSQL smoke before testing any publishing-enabled video flow. Run the bundled S1 and S2 suites with separate test storage.
7. Exercise a short recorded video in no-publisher mode. Enable a connected replay only after checking that the configured destination is the local Parking Management service and that the clip provides the intended lifecycle evidence.
8. Report commands executed, outputs inspected, actual test results and unresolved prerequisites. Distinguish generated event input from real media-driven end-to-end verification. Preserve existing project and database state.
