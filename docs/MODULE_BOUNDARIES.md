# S1 Module Boundaries

## 1. Vehicle Front Processing

Responsibility: detect, track and identify vehicles and collect vehicle evidence.

Existing modules:
- detector.py
- yolo_detector.py
- tracker.py
- vehicles.py
- attributes.py
- plates.py
- plate_crop.py
- plate_evidence.py
- plate_inherit.py
- reid.py
- global_identity.py
- global_reid_manager.py

Responsibilities:
- Vehicle detection and tracking
- Vehicle identity management
- Number plate recognition and validation
- Vehicle attribute extraction
- Plate evidence and association
- OCR confidence and confirmation safeguards

## 2. Parking Slot Processing

Responsibility: determine parking occupancy and manage parking lifecycle.

Existing modules:
- logic.py
- geometry.py
- camera_motion.py
- lifecycle_timeline.py
- exit_restore.py
- determinism.py

Responsibilities:
- Calibrated parking geometry
- Slot assignment
- FREE, OCCUPIED and UNKNOWN states
- Entry tripwire handling
- Parking dwell validation
- PARK_START and PARK_END lifecycle
- Initial occupancy recovery

## 3. Integration Layer

Existing modules:
- pipeline.py
- multicam.py
- multicam_runner.py
- flow.py
- main.py

Responsibilities:
- Connect vehicle observations to parking decisions
- Coordinate multiple cameras
- Maintain vehicle-to-slot associations
- Preserve event ordering and timestamps

## 4. Shared Infrastructure

Existing modules:
- common.py
- config.py
- events.py
- obs.py
- outbox.py
- uploader.py
- s2_mapper.py
- security.py
- report.py
- api.py
- grabber.py

Responsibilities:
- Configuration and validation
- Event contracts and reporting
- Persistence and publishing
- API and observability
- Security and encrypted plate data

## Compatibility requirement

This document defines logical module ownership only.
The existing Python package and import paths remain unchanged.
Physical code separation requires a separately reviewed migration
with import compatibility and regression tests.

## Important lifecycle rules

- OCR failure must not be interpreted as an empty parking slot.
- Occupancy must not depend on successful plate recognition.
- Initial occupants must not receive fabricated ENTRY or PARK_START.
- Live PARK_START requires confirmed entry and configured dwell.
- Events and identifiers must be derived from actual runtime data.
