# Smart Parking S1 setup

The [installation guide](docs/INSTALLATION_GUIDE.md) is the canonical Linux, CPU and model-provisioning reference. For Windows, follow the [PowerShell guide](INSTALL_WINDOWS.md). For GPU, choose a CUDA-compatible PyTorch build and use the GPU dependency list described in the installation guide. CUDA and Windows require validation on the deployment host.

Follow the installation guide’s portable recorded-video preview before configuring site footage or live cameras. The [configuration reference](docs/CONFIGURATION_REFERENCE.md) describes the source, calibration, model and output settings. The [API guide](docs/API_SWAGGER.md) covers launching Uvicorn, sending requests and monitoring or cancelling a job.

Use the [developer verification commands](docs/DEVELOPER_SETUP.md#verify-on-isolated-resources) for both services. The [release reproduction commands](TEST_REPORTS/FINAL_REPRODUCTION_COMMANDS.md) document the historical release. [Lifecycle determinism notes](README_LIFECYCLE_DETERMINISM.md) explain the controlled replay workflow.
