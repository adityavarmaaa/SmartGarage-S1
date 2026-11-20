# Documentation completion

The final [README](../README.md) provides project orientation and links to the canonical setup and operations guides. Repeated setup instructions in README_SETUP and docs/SETUP_GUIDE now point to the [installation guide](../docs/INSTALLATION_GUIDE.md) and [Windows guide](../INSTALL_WINDOWS.md). These identify which installation environments still require validation.

The [architecture](../docs/ARCHITECTURE_HANDOVER.md), [end-to-end guide](../docs/END_TO_END.md), and [configuration reference](../docs/CONFIGURATION_REFERENCE.md) describe camera-local state, geometry, occupancy, lifecycle, ANPR and attribute handling, and the Service 2 mapper and outbox. The [operations guide](../docs/PRODUCTION_OPERATIONS_GUIDE.md), [diagnostics](../docs/ERROR_HANDLING.md), and [troubleshooting](../docs/TROUBLESHOOTING.md) cover startup, monitoring, cancellation, shutdown, output review, and recovery. The [API reference](../docs/API_SWAGGER.md) documents the implemented endpoints, error/status contracts, unsupported latest-frame endpoint, and access limitations.

## Commands and verification

- Executed S1 CLI and local runner help in the existing Linux CPU environment.
- Generated portable preview YAML against the checked-in synthetic evidence video, ran preflight to READY, and ran `main.py` with external publishing and plates disabled. It completed and generated a report.
- Executed loopback Uvicorn requests equivalent to the curl patterns. Exercised recorded video and safe source validation without opening a physical camera. Loaded both documentation UIs in Chromium.
- Ran targeted pytest, full S1, and full S2 with isolated SQLite. See the [API audit](API_AUDIT_REPORT.md) for counts and the [reproduction commands](FINAL_REPRODUCTION_COMMANDS.md) for invocations.
- Checked repository-local Markdown links and repaired three stale historical evidence links. Historical results remain under TEST_REPORTS.

Fresh environment installation, Windows PowerShell, GPU provider selection, GUI calibration, Kaggle, site inputs, and production publishing are reference workflows and were outside this audit.
