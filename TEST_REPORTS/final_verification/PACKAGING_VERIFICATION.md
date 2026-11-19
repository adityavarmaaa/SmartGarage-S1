# Packaging verification

The new release is based on a checksum-verified copy of the previous release. The prior archives, checksums and previous engineering report remain in shared Downloads. The final verification archive keeps project source, calibrated geometry, model, tests, API documentation and this sprint's evidence. Prior operational media and session or restore reports are excluded from this evidence handover. Restore operations that need those historical artifacts require the corresponding authorized local files or the preserved prior release.

The archive includes `ARCHIVE_INVENTORY.csv` with byte counts and SHA-256 values, and `PACKAGE_SHA256_MANIFEST.json`. Embedded manifests omit their own digest to avoid circular references. An external archive checksum covers the complete ZIP, including both manifests. The external packaging report records the exact archive digest and fresh extraction results.
