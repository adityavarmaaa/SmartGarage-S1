import json
import math


class Geometry:
    """Tripwire, slots and plate zone, all in source-frame pixel coordinates."""

    def __init__(self, data):
        self.frame_size = tuple(data.get("frame_size") or (0, 0))
        self.tripwire = [tuple(p) for p in data["tripwire"]]
        self.inside_point = tuple(data["inside_point"])
        self.plate_zone = data.get("plate_zone")
        self.slots = data.get("slots", [])
        # Optional custom motion region (source-frame px). See motion_roi.py.
        roi = data.get("motion_roi")
        self.motion_roi = [tuple(p) for p in roi] if roi else None

    @classmethod
    def load(cls, path):
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f))

    def scaled(self, w, h):
        """Rescale if the stream resolution differs from the calibration resolution."""
        rw, rh = self.frame_size
        if not rw or not rh or (rw, rh) == (w, h):
            return self
        sx, sy = w / rw, h / rh
        sp = lambda p: (p[0] * sx, p[1] * sy)
        return Geometry({
            "frame_size": (w, h),
            "tripwire": [sp(p) for p in self.tripwire],
            "inside_point": sp(self.inside_point),
            "plate_zone": [sp(p) for p in self.plate_zone] if self.plate_zone else None,
            "slots": [{**s, "polygon": [sp(p) for p in s["polygon"]]} for s in self.slots],
            "motion_roi": [sp(p) for p in self.motion_roi] if self.motion_roi else None,
        })


def signed_distance(p1, p2, x, y):
    """Signed perpendicular distance (px) of (x, y) from the line p1->p2."""
    (x1, y1), (x2, y2) = p1, p2
    length = math.hypot(x2 - x1, y2 - y1) or 1.0
    return ((x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)) / length


def assign_ground_point(
    point,
    slots,
    previous_slot=None,
    boundary_margin_px=6.0,
    occupied_slots=None,
    boundary_switch_fraction=0.55,
):
    """Assign from the vehicle ground point; avoid arbitrary boundary ties."""
    import cv2
    import numpy as np

    x, y = float(point[0]), float(point[1])
    occupied = set(occupied_slots or ())
    distances = {}

    for slot in slots:
        sid = slot["slot_id"]
        if sid in occupied and sid != previous_slot:
            continue

        polygon = np.asarray(slot["polygon"], dtype=np.float32).reshape(-1, 1, 2)
        distances[sid] = float(cv2.pointPolygonTest(polygon, (x, y), True))

    margin = max(0.0, float(boundary_margin_px))
    contained = [(sid, distance) for sid, distance in distances.items() if distance >= 0]

    # A point close to an edge is not strong enough to change a previous
    # assignment. If there is no previous assignment, leave it unknown.
    if contained:
        best_distance = max(distance for _, distance in contained)
        best = [sid for sid, distance in contained if best_distance - distance <= 1.0]
        if len(best) != 1:
            return previous_slot if previous_slot in best else None

        chosen = best[0]
        chosen_distance = distances[chosen]
        if chosen_distance < margin:
            previous_distance = distances.get(previous_slot)
            if chosen == previous_slot:
                return previous_slot

            # Keep the prior bay while the point has only just crossed a shared
            # boundary; switch once it penetrates a configurable fraction of
            # the boundary margin. SlotManager adds a time-based confirmation.
            if previous_distance is not None and previous_distance >= 0:
                return previous_slot

            fraction = min(1.0, max(0.0, float(boundary_switch_fraction)))
            if chosen_distance >= margin * fraction:
                return chosen

            if previous_distance is not None and previous_distance >= -margin:
                return previous_slot
            return None
        return chosen

    # A brief gap just outside the previous polygon may be detector jitter.
    if (
        previous_slot is not None
        and previous_slot in distances
        and distances[previous_slot] >= -margin
    ):
        return previous_slot

    return None
