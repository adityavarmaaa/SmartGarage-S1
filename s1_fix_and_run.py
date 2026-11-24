from pathlib import Path
import py_compile
import subprocess
import sys
import shutil

ROOT = Path.cwd()
PIPELINE = ROOT / "vehicle_intelligence" / "pipeline.py"
CONFIG = ROOT / "config" / "config_img2138_runtime.yaml"
LOG = ROOT / "b02_fixed_test.log"

if not PIPELINE.is_file() or not CONFIG.is_file():
    raise SystemExit(
        "ERROR: Open PowerShell in smart_parking_s1_complete project root. "
        "Expected vehicle_intelligence\\pipeline.py and config\\config_img2138_runtime.yaml."
    )

source = PIPELINE.read_text(encoding="utf-8-sig")

init_anchor = """        self.registry = VehicleRegistry()
        self._entry_tripwire_handoffs = {}
"""
init_replacement = init_anchor + """        # Preserve genuine tripwire crossings while a track is still tentative.
        self._pending_tripwire_crossings = {}
"""

old_tripwire_block = """            # Use the existing tripwire's confirmed direction and original
            # crossing timestamp to generate gate ENTRY/EXIT events.
            # Tripwire geometry is calibrated in the same reference frame as
            # the parking polygons, so transform the moving vehicle point into
            # that reference before evaluating its crossing.
            tripwire_point = transform_points(
                [((x1 + x2) / 2.0, y2)], self.camera_transform
            )[0]
            crossing = self.tripwire.update(
                t.id,
                float(tripwire_point[0]),
                float(tripwire_point[1]),
                frame.ts,
            )
"""

insert_anchor = """            self.registry.drop_track(t.id)

        self.last_tracks = tracks
"""
insert_replacement = """            self.registry.drop_track(t.id)

        # ByteTracker returns only confirmed tracks to the parking pipeline.
        # Feed tripwire observations from all tracks updated on this frame,
        # including tentative tracks, so the approach-side state is not lost.
        active_tracker_tracks = [
            track for track in self.tracker.tracks
            if float(track.last_ts) == float(frame.ts)
        ]
        active_tracker_ids = {int(track.id) for track in self.tracker.tracks}
        for stale_id in list(self.tripwire.state):
            if int(stale_id) not in active_tracker_ids:
                self.tripwire.forget(stale_id)

        for observed_track in active_tracker_tracks:
            ox1, oy1, ox2, oy2 = map(float, observed_track.bbox)
            tripwire_point = transform_points(
                [((ox1 + ox2) / 2.0, oy2)], self.camera_transform
            )[0]
            observed_crossing = self.tripwire.update(
                observed_track.id,
                float(tripwire_point[0]),
                float(tripwire_point[1]),
                frame.ts,
            )
            if (
                observed_crossing is not None
                and int(observed_track.id) not in self._pending_tripwire_crossings
            ):
                self._pending_tripwire_crossings[int(observed_track.id)] = observed_crossing

        self.last_tracks = tracks
"""

new_crossing_block = """            # Crossing evidence was collected from the track's first
            # detector observations, including its tentative period.
            crossing = self._pending_tripwire_crossings.pop(int(t.id), None)
"""

if "self._pending_tripwire_crossings = {}" in source and "active_tracker_tracks = [" in source:
    print("Tripwire fix is already present; skipping source modification.")
else:
    counts = {
        "registry init": source.count(init_anchor),
        "original tripwire block": source.count(old_tripwire_block),
        "track cleanup anchor": source.count(insert_anchor),
    }
    if counts != {
        "registry init": 1,
        "original tripwire block": 1,
        "track cleanup anchor": 1,
    }:
        raise SystemExit(
            "ERROR: Active source differs from the reviewed version; no edits made. "
            f"Anchor counts: {counts}"
        )

    patched = source.replace(init_anchor, init_replacement, 1)
    patched = patched.replace(insert_anchor, insert_replacement, 1)
    patched = patched.replace(old_tripwire_block, new_crossing_block, 1)

    if patched.count("self.tripwire.update(") != 1:
        raise SystemExit("ERROR: Patch validation failed; original file left unchanged.")

    compile(patched, str(PIPELINE), "exec")
    backup = PIPELINE.with_name("pipeline.py.before_tentative_tripwire_fix.bak")
    if not backup.exists():
        shutil.copy2(PIPELINE, backup)
    PIPELINE.write_text(patched, encoding="utf-8")
    py_compile.compile(str(PIPELINE), doraise=True)
    print(f"PATCH APPLIED; backup saved to: {backup}")

print("Starting Service 1 replay using config_img2138_runtime.yaml ...")
print(f"Full log: {LOG}")
with LOG.open("w", encoding="utf-8") as log_file:
    process = subprocess.Popen(
        [
            sys.executable, "-m", "vehicle_intelligence.main",
            "--config", str(CONFIG),
            "--replay", "sync",
            "--no-publisher",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="")
        log_file.write(line)
        log_file.flush()
    code = process.wait()

print(f"\nService 1 replay exit code: {code}")
print(f"Full log saved: {LOG}")
if code != 0:
    raise SystemExit(code)
print("\nReplay finished. Review B-02 occupancy, ENTRY, PARK_START and PLATE_STATE in the report/log.")
