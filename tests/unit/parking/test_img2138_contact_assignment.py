import json
from pathlib import Path

from s1.logic import SlotManager


def test_img2138_lower_body_contact_assigns_middle_bay_from_calibrated_geometry():
    root = Path(__file__).resolve().parents[3]
    geometry = json.loads(
        (root / "data" / "geometry_img2138_4slots.json").read_text(encoding="utf-8")
    )
    cfg = {
        "footprint_frac": 0.25,
        "slot_boundary_margin_px": 6.0,
        "slot_boundary_switch_fraction": 0.55,
        "slot_overlap_fallback_min": 0.35,
        "slot_overlap_fallback_margin": 0.15,
        "contact_point_fractions": [0.0, 0.08, 0.16, 0.24, 0.32],
    }
    manager = SlotManager(geometry["slots"], tuple(geometry["frame_size"]), cfg)

    # Detector box includes pixels below the actual wheel-contact region.
    # The deeper calibrated lower-body sample lies inside the middle polygon.
    assert manager.contact_slot((290, 850, 650, 1160), track_id=4) == "B-02"
