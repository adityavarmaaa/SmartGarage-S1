import math
from collections import deque
from dataclasses import dataclass

import cv2
import numpy as np

import logging
log = logging.getLogger("s1.logic")
from .slot_geometry import signed_distance, assign_ground_point
from .camera_motion import transform_bbox, transform_points

def still_run_start(hist, still_frac):
    """Return the start time of the current stationary run."""
    if not hist:
        return 0.0

    latest_ts, latest_x, latest_y, latest_w = hist[-1]
    tolerance = float(still_frac) * max(float(latest_w), 1.0)
    start_ts = latest_ts

    for sample in reversed(hist):
        sample_ts, sample_x, sample_y, _ = sample

        if math.hypot(
            float(sample_x) - float(latest_x),
            float(sample_y) - float(latest_y),
        ) > tolerance:
            break

        start_ts = sample_ts

    return float(start_ts)


class Tripwire:
    """Classify crossings using apparent vehicle-size change when supplied.

    Growing apparent size indicates entry; shrinking size indicates
    exit. If size evidence is inconclusive, no direction is emitted.
    Calls without size retain the positional legacy behavior for compatibility.
    """

    def __init__(
        self,
        p1,
        p2,
        inside_point,
        margin_px=12.0,
        size_samples=3,
        min_size_change=0.15,
    ):
        self.p1, self.p2, self.margin = p1, p2, float(margin_px)
        self.inside_side = (
            1 if signed_distance(p1, p2, *inside_point) > 0 else -1
        )
        self.size_samples = max(2, int(size_samples))
        self.min_size_change = max(0.0, float(min_size_change))
        self.state = {}

    def _new_state(self):
        return {
            "side": 0,
            "last_d": None,
            "last_ts": None,
            "sizes": [],
            "candidate": None,
        }

    def update(self, tid, x, y, ts, size=None):
        d = signed_distance(self.p1, self.p2, x, y)
        st = self.state.setdefault(tid, self._new_state())

        if size is None:
            old_d, old_ts = st["last_d"], st["last_ts"]
            st["last_d"], st["last_ts"] = d, ts

            side = (
                1
                if d > self.margin
                else (-1 if d < -self.margin else 0)
            )

            if not side:
                return None

            previous = st["side"]
            st["side"] = side

            if previous and previous != side:
                cross_ts = ts

                if (
                    old_d is not None
                    and old_ts is not None
                    and old_d != d
                ):
                    cross_ts = (
                        old_ts
                        + old_d / (old_d - d) * (ts - old_ts)
                    )

                return (
                    "entry" if side == self.inside_side else "exit",
                    cross_ts,
                )

            return None

        size = float(size)

        if size > 0:
            st["sizes"].append((float(ts), size))
            st["sizes"] = st["sizes"][-120:]

        candidate = st["candidate"]

        if candidate is not None and ts > candidate["cross_ts"] and size > 0:
            candidate["after"].append(size)

            if len(candidate["after"]) >= self.size_samples:
                st["candidate"] = None

                before = candidate["before"]
                after = candidate["after"][: self.size_samples]

                if len(before) < self.size_samples:
                    st["last_d"], st["last_ts"] = d, ts
                    return None

                before_size = float(np.median(before))
                after_size = float(np.median(after))

                change = (
                    (after_size - before_size) / before_size
                    if before_size > 0
                    else 0.0
                )

                st["last_d"], st["last_ts"] = d, ts

                if abs(change) >= self.min_size_change:
                    return candidate["direction"], candidate["cross_ts"]


                return None

        side = (
            1
            if d > self.margin
            else (-1 if d < -self.margin else 0)
        )

        if side:
            previous = st["side"]
            st["side"] = side

            if previous and previous != side:
                old_d, old_ts = st["last_d"], st["last_ts"]
                cross_ts = ts

                if (
                    old_d is not None
                    and old_ts is not None
                    and old_d != d
                ):
                    cross_ts = (
                        old_ts
                        + old_d / (old_d - d) * (ts - old_ts)
                    )

                before = [
                    sz
                    for sample_ts, sz in st["sizes"]
                    if sample_ts < cross_ts
                ]

                st["candidate"] = {
                    "cross_ts": cross_ts,
                    "before": before[-self.size_samples :],
                    "after": [],
                    "direction": "entry" if side == self.inside_side else "exit",
                }

        st["last_d"], st["last_ts"] = d, ts
        if st["candidate"] is not None:
            log.debug(
                "TRIPWIRE_CANDIDATE tid=%s cross_ts=%.3f before=%s after=%s",
                tid,
                st["candidate"]["cross_ts"],
                st["candidate"]["before"],
                st["candidate"]["after"],
            )
        return None

    def forget(self, tid):
        self.state.pop(tid, None)


@dataclass
class Occupant:
    vehicle: object
    track_id: object
    parked_at: float
    last_seen: float
    lost_since: object = None
    baseline: bool = False
    occupancy_source: str = "LIVE_ENTRY"
    restoration_pending: bool = False


class SlotManager:
    """Predefined bays -> PARK_START / PARK_END per vehicle.

    PARKED  = footprint inside a bay polygon AND stationary for park_dwell_s.
              Event time is BACKDATED to when the vehicle actually stopped.
    LEFT    = out of the bay / moved away for leave_dwell_s.
              Event time is BACKDATED to the last moment it was at rest in the bay.
    If a parked vehicle's track is lost (occlusion), the bay is held for
    lost_grace_s and a new stationary track in the same bay inherits the identity.
    """

    def __init__(self, slots, frame_size, cfg):
        w, h = int(frame_size[0]), int(frame_size[1])
        self.w, self.h, self.cfg = w, h, cfg
        self.slots = {}

        for s in slots:
            mask = np.zeros((h, w), np.uint8)
            cv2.fillPoly(
                mask,
                [np.array(s["polygon"], np.int32)],
                1,
            )

            self.slots[s["slot_id"]] = {
                "slot_id": s["slot_id"],
                "level": s.get("level"),
                "mask": mask,
                "polygon": np.asarray(s["polygon"], dtype=np.float32),
            }

        self.tstate = {}
        self.occupants = {}
        self.stream_start_ts = None
        self.camera_transform = np.array([[1., 0., 0.], [0., 1., 0.]], dtype=np.float32)

    def set_camera_transform(self, current_to_reference):
        """Set the current-frame -> reference-frame affine transform."""
        matrix = np.asarray(current_to_reference, dtype=np.float32)
        if matrix.shape == (2, 3) and np.isfinite(matrix).all():
            self.camera_transform = matrix.copy()

    def _record_reference_history(self, track, ts, state):
        """Record the current detection in calibrated-reference coordinates.

        Track.history is in per-frame screen pixels. Using it directly for
        stationary/departure tests breaks as soon as the camera pans or zooms.
        Store the transformed ground point with the transform from THIS frame.
        """
        history = state.setdefault("_reference_history", deque())
        x1, y1, x2, y2 = map(float, track.bbox)
        point = transform_points([((x1 + x2) / 2.0, y2)], self.camera_transform)[0]
        ref_box = transform_bbox(track.bbox, self.camera_transform)
        width = max(1.0, float(ref_box[2] - ref_box[0]))
        sample = (float(ts), float(point[0]), float(point[1]), width)

        if history and abs(float(history[-1][0]) - float(ts)) < 1e-6:
            history[-1] = sample
        else:
            history.append(sample)
        while history and float(ts) - float(history[0][0]) > 120.0:
            history.popleft()
        return history

    def slot_info(self, sid):
        s = self.slots[sid]
        return {
            "slot_id": sid,
            "level": s["level"],
        }

    def slot_states(self):
        """Return one FREE/OCCUPIED record for every calibrated slot."""
        states = []
        for sid, slot in self.slots.items():
            occ = self.occupants.get(sid)
            states.append({
                "slot_id": sid,
                "level": slot["level"],
                "state": "OCCUPIED" if occ is not None else "FREE",
                "track_id": occ.track_id if occ is not None else None,
                "vehicle_uid": occ.vehicle.uid if occ is not None else None,
                "occupancy_source": occ.occupancy_source if occ is not None else None,
            })
        return states

    def overlaps(self, bbox):
        # Slot masks are in reference-frame coordinates; map the detection box
        # into that same space before measuring its lower-footprint overlap.
        x1, y1, x2, y2 = transform_bbox(bbox, self.camera_transform)
        fy1 = y2 - self.cfg["footprint_frac"] * (y2 - y1)

        rx1 = int(max(0, x1))
        rx2 = int(min(self.w, math.ceil(x2)))
        ry1 = int(max(0, fy1))
        ry2 = int(min(self.h, math.ceil(y2)))

        area = (rx2 - rx1) * (ry2 - ry1)

        if area <= 0:
            return {}

        return {
            sid: cv2.countNonZero(
                s["mask"][ry1:ry2, rx1:rx2]
            ) / area
            for sid, s in self.slots.items()
        }


    def contact_slot(self, bbox, previous_slot=None, track_id=None):
        """Assign by bottom-centre ground point mapped into reference space."""
        x1, y1, x2, y2 = map(float, bbox)
        # Test the true bottom-centre first, then nearby lower-body
        # points to tolerate small detector-box padding errors.
        height = max(1.0, y2 - y1)
        contact_fractions = self.cfg.get(
            "contact_point_fractions", [0.0, 0.08, 0.16]
        )
        # Keep the configured order and ignore invalid/duplicate samples.
        fractions = []
        for value in contact_fractions:
            try:
                fraction = float(value)
            except (TypeError, ValueError):
                continue
            if 0.0 <= fraction <= 0.50 and fraction not in fractions:
                fractions.append(fraction)
        if not fractions:
            fractions = [0.0, 0.08, 0.16]
        source_points = [
            ((x1 + x2) / 2.0, y2 - fraction * height)
            for fraction in fractions
        ]
        points = transform_points(source_points, self.camera_transform)
        occupied_by_other = {
            sid for sid, occ in self.occupants.items()
            if occ.track_id is not None and occ.track_id != track_id
        }
        # Only confirmed track slot claims reserve a bay here.
        # Unconfirmed candidates must not block another track's valid contact.
        for other_track_id, state in self.tstate.items():
            if other_track_id == track_id:
                continue
            claimed = state.get("slot")
            if claimed is not None:
                occupied_by_other.add(claimed)
        slots = [{"slot_id": sid, "polygon": slot["polygon"]}
                 for sid, slot in self.slots.items()]
        for point in points:
            assigned = assign_ground_point(
                point, slots, previous_slot=previous_slot,
                boundary_margin_px=float(self.cfg.get("slot_boundary_margin_px", 8.0)),
                occupied_slots=occupied_by_other,
                boundary_switch_fraction=float(
                    self.cfg.get("slot_boundary_switch_fraction", 0.55)
                ),
            )
            if assigned is not None:
                return assigned

        # Conservative fallback for detector boxes whose lower edge is padded
        # or clipped: accept a slot only when its calibrated lower-footprint
        # overlap is both strong and clearly separated from every other bay.
        # This is deliberately NOT a "best slot wins" fallback.
        overlap_min = max(
            0.0, float(self.cfg.get("slot_overlap_fallback_min", 0.35))
        )
        overlap_margin = max(
            0.0, float(self.cfg.get("slot_overlap_fallback_margin", 0.15))
        )
        overlap_scores = {
            sid: float(score)
            for sid, score in self.overlaps(bbox).items()
            if sid not in occupied_by_other
        }
        ranked = sorted(overlap_scores.items(), key=lambda item: item[1], reverse=True)
        if ranked:
            best_sid, best_score = ranked[0]
            runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
            if (
                best_score >= overlap_min
                and best_score - runner_up >= overlap_margin
            ):
                return best_sid
        return None

    def recover_lost_track(self, track, ts, registry):
        """Recover a parked vehicle's identity before tripwire processing.

        When a parked vehicle temporarily loses its tracker ID, the old
        Vehicle object remains attached to its occupied slot. If a new
        tracker ID appears again inside that same occupied slot while the
        old occupant is still within the lost grace period, restore the
        old Vehicle identity before any ENTRY/EXIT event is generated.

        Returns True when a recovery was performed.
        """

        # Recover identity only from the track's ground-contact point.
        # Whole-box overlap can span neighboring bays in an angled camera view.
        sid = self.contact_slot(track.bbox, track_id=track.id)
        if sid is None:
            return False

        occ = self.occupants.get(sid)

        if occ is None:
            return False

        if occ.track_id is not None:
            return False

        if occ.lost_since is None:
            return False

        if (
            ts - occ.lost_since
            >= self.cfg["lost_grace_s"]
        ):
            return False

        vehicle = occ.vehicle

        # Rebind the new tracker ID to the original Vehicle object.
        registry.rebind(track.id, vehicle)

        # Treat the recovered track as already occupying this slot. The anchor
        # is stored in geometry/reference coordinates, just like all movement
        # checks below.
        recovered_state = {"_reference_history": deque()}
        hist = self._record_reference_history(track, ts, recovered_state)
        cx, cy = hist[-1][1], hist[-1][2]
        recovered_state.update({
            "slot": sid,
            "candidate_slot": None,
            "candidate_since": None,
            "candidate_last_seen": None,
            "pending_slot": None,
            "pending_slot_since": None,
            "anchor": (cx, cy),
            "depart_since": None,
            "baseline_slot": None,
            "baseline_frac": 0.0,
        })
        self.tstate[track.id] = recovered_state

        occ.track_id = track.id
        occ.lost_since = None
        occ.last_seen = ts
        occ.restoration_pending = False

        return True

    def _confirmed_baseline_slot(self, track_id, state, ts):
        """Return a previously confirmed startup slot after the scan window.

        Once the baseline scan has confirmed a vehicle's slot, later detector
        box jitter must not erase that decision. The remembered assignment is
        still rejected if another track/occupant currently owns the bay.
        """
        sid = state.get("baseline_slot")
        if sid is None or self.stream_start_ts is None:
            return None

        baseline_window = max(0.0, float(self.cfg.get("baseline_window_s", 2.0)))
        if float(ts) - float(self.stream_start_ts) <= baseline_window:
            return None
        if sid not in self.slots:
            return None

        occupant = self.occupants.get(sid)
        if occupant is not None and occupant.track_id != track_id:
            return None

        for other_track_id, other_state in self.tstate.items():
            if other_track_id == track_id:
                continue
            if sid in (other_state.get("slot"), other_state.get("candidate_slot")):
                return None

        return sid

    def update(self, tracks, ts, registry):
        """Returns list of (event_type, vehicle, event_time, slot_id, inferred)."""
        c, events = self.cfg, []

        if self.stream_start_ts is None:
            self.stream_start_ts = float(ts)

        for t in tracks:
            st = self.tstate.setdefault(
                t.id,
                {
                    "slot": None,
                    "candidate_slot": None,
                    "candidate_since": None,
                    "candidate_last_seen": None,
                    "pending_slot": None,
                    "pending_slot_since": None,
                    "anchor": None,
                    "depart_since": None,
                    "baseline_slot": None,
                    "baseline_frac": 0.0,
                    "_reference_history": deque(),
                },
            )

            hist = self._record_reference_history(t, ts, st)
            cx, cy, w = hist[-1][1], hist[-1][2], hist[-1][3]

            ov = self.overlaps(t.bbox)

            if st["slot"] is None:
                # Ground-point assignment is authoritative. Whole-box overlap
                # remains a dwell/footprint quality gate, not a slot selector.
                # A confirmed INITIAL_SCAN slot is authoritative after the
                # startup window. Do not lose it merely because a later bbox
                # contact sample becomes temporarily ambiguous.
                remembered_baseline_sid = self._confirmed_baseline_slot(
                    t.id, st, ts
                )
                if remembered_baseline_sid is not None:
                    contact_sid = remembered_baseline_sid
                    st["candidate_slot"] = remembered_baseline_sid
                    st["candidate_since"] = st.get("candidate_since") or ts
                    st["candidate_last_seen"] = ts
                    st["pending_slot"] = None
                    st["pending_slot_since"] = None
                else:
                    contact_sid = self.contact_slot(
                        t.bbox, previous_slot=st.get("candidate_slot"), track_id=t.id
                    )
                # Remember whether the slot was assigned from the ground point.
                # A valid contact point is stronger evidence than whole-box
                # overlap, which can be low for narrow/perspective-distorted bays.
                ground_point_assigned = contact_sid is not None

                # Ground-contact assignment is the only slot selector.
                # Box overlap remains a dwell-quality signal; it must never
                # invent a neighboring slot when the contact point is unknown.
                ground_point_assigned = contact_sid is not None

                candidate = st.get("candidate_slot")
                gap_grace_s = max(0.0, float(c.get("candidate_gap_grace_s", 0.6)))
                switch_confirm_s = max(0.0, float(c.get("candidate_switch_confirm_s", 0.6)))

                try:
                    st["_dg"] = st.get("_dg", 0) + 1
                    if st["_dg"] % 5 == 1:
                        _x1, _y1, _x2, _y2 = map(float, t.bbox)
                        _pt = transform_points([((_x1 + _x2) / 2.0, _y2)], self.camera_transform)[0]
                        _top = sorted(ov.items(), key=lambda kv: -kv[1])[:3]
                        __import__("logging").getLogger("s1.logic").info(
                            "SLOT_DIAG track=%s ts=%.2f bbox=(%.0f,%.0f,%.0f,%.0f) ground_ref=(%.0f,%.0f) contact=%s top=%s",
                            t.id, ts, _x1, _y1, _x2, _y2, float(_pt[0]), float(_pt[1]), contact_sid,
                            [(k, round(v, 2)) for k, v in _top])
                except Exception:
                    pass
                if contact_sid is None:
                    # Ambiguous slot evidence cannot advance parking dwell.
                    # Preserve a candidate briefly for detector jitter, but
                    # require fresh dwell if the ambiguity lasts too long.
                    last_seen = st.get("candidate_last_seen")
                    if (
                        candidate is not None
                        and last_seen is not None
                        and ts - last_seen <= gap_grace_s
                    ):
                        st["pending_slot"] = None
                        st["pending_slot_since"] = None
                        continue

                    st["candidate_slot"] = None
                    st["candidate_since"] = None
                    st["candidate_last_seen"] = None
                    st["pending_slot"] = None
                    st["pending_slot_since"] = None
                    # Reset only an incomplete vote sequence. Preserve a
                    # previously confirmed INITIAL_SCAN assignment.
                    st["baseline_vote_slot"] = None
                    st["baseline_slot_votes"] = 0
                    st["baseline_frac_sum"] = 0.0
                    continue

                proposed_sid = contact_sid
                if candidate is None:
                    st["candidate_slot"] = proposed_sid
                    st["candidate_since"] = ts
                    st["candidate_last_seen"] = ts
                    st["pending_slot"] = None
                    st["pending_slot_since"] = None
                    candidate = proposed_sid
                elif candidate != proposed_sid:
                    # Require a different bay assignment to persist before
                    # switching candidates. This filters one-frame polygon
                    # boundary jitter without letting the old bay confirm.
                    pending = st.get("pending_slot")
                    if pending != proposed_sid:
                        st["pending_slot"] = proposed_sid
                        st["pending_slot_since"] = ts
                        continue
                    pending_since = st.get("pending_slot_since")
                    if pending_since is None or ts - pending_since < switch_confirm_s:
                        continue
                    st["candidate_slot"] = proposed_sid
                    st["candidate_since"] = ts
                    st["candidate_last_seen"] = ts
                    st["pending_slot"] = None
                    st["pending_slot_since"] = None
                    candidate = proposed_sid
                else:
                    st["candidate_last_seen"] = ts
                    st["pending_slot"] = None
                    st["pending_slot_since"] = None

                sid = proposed_sid
                frac = float(ov.get(sid, 0.0))

                start = still_run_start(
                    hist,
                    c["still_frac"],
                )

                # Baseline EXIT clip: the stream begins with a vehicle
                # already parked. Do not require the normal parking dwell.
                baseline_window = max(0.0, float(c.get("baseline_window_s", 2.0)))
                baseline_vehicle = registry.by_track.get(t.id)
                has_accepted_entry = (
                    getattr(baseline_vehicle, "entry_crossing_ts", None) is not None
                )
                first_seen_offset = (
                    float(getattr(t, "first_ts", ts)) - float(self.stream_start_ts)
                    if self.stream_start_ts is not None else -1.0
                )
                baseline_candidate = (
                    self.stream_start_ts is not None
                    and not has_accepted_entry
                    and 0.0 <= first_seen_offset <= max(
                        baseline_window,
                        float(c.get("baseline_first_seen_tolerance_s", baseline_window))
                    )
                    and (
                        ts - self.stream_start_ts <= baseline_window
                        or (
                            int(st.get("baseline_slot_votes", 0)) > 0
                            and st.get("baseline_slot") is None
                        )
                    )
                )

                # Focused real-video diagnostics: explain whether this track
                # is eligible for INITIAL_SCAN and whether geometry/votes/dwell
                # are preventing confirmation. Throttle per track to avoid log spam.
                try:
                    st["_baseline_diag_n"] = int(st.get("_baseline_diag_n", 0)) + 1
                    if st["_baseline_diag_n"] % 5 == 1:
                        _first_ts = float(getattr(t, "first_ts", ts))
                        _stream_ts = float(self.stream_start_ts) if self.stream_start_ts is not None else None
                        _first_offset = (_first_ts - _stream_ts) if _stream_ts is not None else None
                        _stationary_now = max(0.0, float(ts) - float(start))
                        __import__("logging").getLogger("s1.logic").info(
                            "BASELINE_DIAG track=%s slot=%s ts=%.3f first_ts=%.3f first_offset=%s stream_start=%s window=%.2f eligible=%s contact=%s overlap=%.3f stationary=%.2f votes=%s required=%s",
                            t.id, sid, float(ts), _first_ts,
                            ("%.3f" % _first_offset) if _first_offset is not None else "none",
                            ("%.3f" % _stream_ts) if _stream_ts is not None else "none",
                            baseline_window, baseline_candidate, ground_point_assigned,
                            frac, _stationary_now,
                            st.get("baseline_slot_votes", 0),
                            max(2, int(c.get("baseline_confirm_frames", 3))),
                        )
                except Exception:
                    pass

                if baseline_candidate:
                    # During the startup scan, the ground-contact point inside
                    # a calibrated bay is authoritative. Whole-box overlap can
                    # be low for perspective-distorted/narrow bays (notably
                    # cars already parked at startup), so do not reject a
                    # baseline occupant solely because its bbox overlap is low.
                    if frac < 0.20 and not ground_point_assigned:
                        continue

                    # Confirm repeated observations of the same geometry-derived slot.
                    if st.get("baseline_vote_slot") != sid:
                        st["baseline_vote_slot"] = sid
                        st["baseline_slot_votes"] = 1
                        st["baseline_frac_sum"] = float(frac)
                    else:
                        st["baseline_slot_votes"] = int(st.get("baseline_slot_votes", 0)) + 1
                        st["baseline_frac_sum"] = float(st.get("baseline_frac_sum", 0.0)) + float(frac)

                    required_votes = max(2, int(c.get("baseline_confirm_frames", 3)))
                    baseline_stationary_s = max(
                        0.0, float(c.get("baseline_stationary_s", 0.75))
                    )
                    stationary_dwell = max(0.0, float(ts) - float(start))

                    if (
                        st["baseline_slot_votes"] >= required_votes
                        and stationary_dwell >= baseline_stationary_s
                    ):
                        st["baseline_slot"] = sid
                        st["baseline_frac"] = (
                            st["baseline_frac_sum"] / st["baseline_slot_votes"]
                        )
                    else:
                        st["baseline_slot"] = None
                        st["baseline_frac"] = 0.0

                    continue

                # After the opening baseline window, register the remembered
                # occupant immediately. Normal arriving vehicles still use
                # the existing 4-second dwell path.
                if st.get("baseline_slot") is not None:
                    sid = st["baseline_slot"]
                    frac = st["baseline_frac"]
                    st["baseline_slot"] = None
                    st["baseline_frac"] = 0.0
                    baseline_candidate = True
                else:
                    baseline_candidate = False

                if not baseline_candidate:
                    v_for_entry = registry.by_track.get(t.id)
                    entry_crossing_ts = getattr(
                        v_for_entry, "entry_crossing_ts", None
                    )

                    if entry_crossing_ts is None:
                        log.debug(
                            "PARK_START_WAIT track=%s slot=%s "
                            "reason=entry_tripwire_not_confirmed",
                            t.id, sid,
                        )
                        continue

                    # Require overlap only for the fallback assignment path.
                    # When the ground-contact point is inside a calibrated bay,
                    # that geometric evidence is authoritative; overlap can be
                    # below 0.30 simply because the visible car box straddles a
                    # narrow/perspective bay boundary.
                    if frac < float(c["min_overlap"]) and not ground_point_assigned:
                        log.debug(
                            "PARK_START_WAIT track=%s slot=%s reason=overlap "
                            "overlap=%.3f required=%.3f ground_point=%s",
                            t.id, sid, frac, float(c["min_overlap"]),
                            ground_point_assigned,
                        )
                        continue

                    # Confirm both (1) stationary duration and (2) duration
                    # assigned to this exact bay. Use the later start as the
                    # parking event time so a car that waited outside the bay
                    # is not backdated to the beginning of that earlier wait.
                    candidate_since = st.get("candidate_since")
                    slot_dwell_start = (
                        candidate_since if candidate_since is not None else start
                    )
                    slot_dwell = max(
                        0.0,
                        ts - max(float(slot_dwell_start), float(entry_crossing_ts)),
                    )
                    stationary_dwell = max(
                        0.0,
                        ts - max(float(start), float(entry_crossing_ts)),
                    )
                    required_dwell = float(c["park_dwell_s"])

                    if (
                        slot_dwell < required_dwell
                        or stationary_dwell < required_dwell
                    ):
                        log.debug(
                            "PARK_START_WAIT track=%s slot=%s reason=slot_dwell "
                            "overlap=%.3f dwell=%.2fs stationary=%.2fs required=%.2fs history=%d",
                            t.id, sid, frac, slot_dwell, stationary_dwell,
                            required_dwell, len(hist),
                        )
                        continue

                    parking_start_ts = max(
                        float(start),
                        float(slot_dwell_start),
                        float(entry_crossing_ts),
                    )
                else:
                    parking_start_ts = float(start)

                occ = self.occupants.get(sid)

                if occ is not None and occ.track_id is None:
                    # Lost vehicle reacquired: preserve its identity.
                    registry.rebind(t.id, occ.vehicle)

                    occ.track_id = t.id
                    occ.lost_since = None
                    occ.last_seen = ts
                    occ.restoration_pending = False

                    st.update(
                        slot=sid,
                        anchor=(cx, cy),
                        depart_since=None,
                    )

                elif occ is not None:
                    continue

                else:
                    v = registry.by_track[t.id]

                    baseline_window = max(
                        0.0,
                        float(c.get("baseline_window_s", 2.0)),
                    )
                    # Preserve the initial-scan decision. The stationary-history
                    # start may be later than stream start even for a vehicle
                    # confirmed as present during the baseline scan.
                    is_baseline = bool(baseline_candidate)
                    self.occupants[sid] = Occupant(
                        v,
                        t.id,
                        (float(start) if is_baseline else parking_start_ts),
                        ts,
                        baseline=is_baseline,
                        occupancy_source=("INITIAL_SCAN" if is_baseline else "LIVE_ENTRY"),
                    )

                    v.slot_id = sid

                    st.update(
                        slot=sid,
                        anchor=(cx, cy),
                        depart_since=None,
                    )

                    if is_baseline:
                        v.has_parked = True
                        log.info(
                            "BASELINE_OCCUPANT slot=%s vehicle=%s "
                            "parked_at=%.3f; ENTRY suppressed",
                            sid,
                            v.uid,
                            start,
                        )
                    else:
                        events.append(
                            (
                                "PARK_START",
                                v,
                                parking_start_ts,
                                sid,
                                False,
                            )
                        )

            else:
                sid = st["slot"]
                occ = self.occupants[sid]

                occ.last_seen = ts

                ax, ay = st["anchor"]

                overlap = float(ov.get(sid, 0.0))
                leave_overlap = float(c["leave_overlap"])

                moved = (
                    math.hypot(cx - ax, cy - ay)
                    > c["moved_frac"] * w
                )

                current_contact_sid = self.contact_slot(
                    t.bbox, previous_slot=sid, track_id=t.id
                )
                # Motion inside a bay is not a departure. Require meaningful
                # movement plus ground-contact evidence that the vehicle has
                # left this bay (or has moved far enough that overlap collapses).
                outside_slot = current_contact_sid != sid
                # Begin a time-based departure confirmation when the ground
                # point has left this bay and the lower vehicle footprint no
                # longer overlaps it sufficiently. Requiring `moved` first made
                # a wide bbox keep a bay occupied forever even after overlap=0.
                leaving = outside_slot and overlap < leave_overlap

                log.debug(
                    "OCCUPANCY_DIAG track=%s uid=%s slot=%s overlap=%.3f "
                    "moved=%s outside=%s anchor=(%.1f,%.1f) current=(%.1f,%.1f) "
                    "contact=%s depart_since=%s",
                    t.id, occ.vehicle.uid, sid, overlap,
                    moved, outside_slot, ax, ay, cx, cy, current_contact_sid,
                    st["depart_since"],
                )

                if not leaving:
                    st["depart_since"] = None
                    # Keep the parked-position anchor stable. Refreshing it on
                    # every frame lets a slowly departing vehicle drag the
                    # anchor along with it, so cumulative movement is never
                    # large enough to trigger PARK_END. The anchor is set when
                    # the vehicle becomes OCCUPIED and tolerates detector jitter
                    # through moved_frac.
                    continue

                if st["depart_since"] is None:
                    st["depart_since"] = ts


                if (
                    ts - st["depart_since"]
                    >= c["leave_dwell_s"]
                ):
                    last_rest = st["depart_since"]

                    for hts, hx, hy, _ in reversed(hist):
                        if (
                            math.hypot(hx - ax, hy - ay)
                            <= c["still_frac"] * w
                        ):
                            last_rest = hts
                            break

                    events.append(
                        (
                            "PARK_END",
                            occ.vehicle,
                            last_rest,
                            sid,
                            False,
                        )
                    )

                    occ.vehicle.slot_id = None
                    del self.occupants[sid]

                    st.update(
                        slot=None,
                        anchor=None,
                        depart_since=None,
                    )

        events += self.expire(ts)
        return events

    def on_track_removed(self, track):
        st = self.tstate.pop(track.id, None)

        if st and st["slot"]:
            occ = self.occupants.get(st["slot"])

            if occ and occ.track_id == track.id:
                occ.track_id = None
                occ.lost_since = occ.last_seen

    def expire(self, ts):
        events = []

        for sid, occ in list(self.occupants.items()):
            if (
                occ.track_id is None
                and not getattr(occ, "restoration_pending", False)
                and occ.lost_since is not None
                and ts - occ.lost_since
                >= self.cfg["lost_grace_s"]
            ):
                events.append(
                    (
                        "PARK_END",
                        occ.vehicle,
                        occ.lost_since,
                        sid,
                        True,
                    )
                )

                occ.vehicle.slot_id = None
                del self.occupants[sid]

        return events