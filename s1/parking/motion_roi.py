"""Parking-area motion mask + small-blob filter for the pre-YOLO motion gate.

Why this exists
---------------
The original activity score looked at the WHOLE frame, so leaves, birds, sky and
people far outside the garage could wake the camera. Here we
  1. restrict motion to the parking area (hull of slots + tripwire + inside point,
     padded by a margin so a car arriving at the tripwire is still seen), and
  2. ignore tiny scattered changes (leaves / birds / sensor noise): only
     connected changed-regions of at least `min_blob_px` pixels (measured at the
     160x90 analysis resolution) count as motion.

A custom polygon can be supplied with `"motion_roi": [[x,y], ...]` in the
camera's geometry JSON (source-frame pixel coordinates). It overrides the hull.
"""
import logging

import cv2
import numpy as np

log = logging.getLogger("motion_roi")

ANALYSIS_SIZE = (160, 90)  # (w, h) used by multicam._activity_score


def _scaled_points(points, sx, sy):
    return [(float(p[0]) * sx, float(p[1]) * sy) for p in points]


def build_motion_mask(geometry, frame_wh, size=ANALYSIS_SIZE, margin_frac=0.06,
                      min_area_frac=0.02):
    """Return (mask uint8 0/255 of shape (h, w), info dict) or (None, info).

    geometry : s1.geometry.Geometry (calibration-resolution coordinates)
    frame_wh : (width, height) of the actual stream
    """
    w, h = int(size[0]), int(size[1])
    fw, fh = float(frame_wh[0]), float(frame_wh[1])
    info = {"enabled": False, "source": None, "area_frac": None}

    # Work in calibration coordinates, then scale to the analysis grid.
    cw, ch = geometry.frame_size if geometry.frame_size and all(geometry.frame_size) else (fw, fh)
    sx, sy = w / float(cw), h / float(ch)

    custom = getattr(geometry, "motion_roi", None)
    if custom and len(custom) >= 3:
        pts = _scaled_points(custom, sx, sy)
        info["source"] = "geometry.motion_roi"
    else:
        pts = []
        for slot in geometry.slots:
            pts += _scaled_points(slot["polygon"], sx, sy)
        pts += _scaled_points(geometry.tripwire, sx, sy)
        pts += _scaled_points([geometry.inside_point], sx, sy)
        info["source"] = "hull(slots+tripwire+inside_point)"

    if len(pts) < 3:
        return None, info

    hull = cv2.convexHull(np.array(pts, dtype=np.float32)).astype(np.int32)
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.fillConvexPoly(mask, hull, 255)

    k = int(round(max(1.0, margin_frac * max(w, h))))
    if k > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 1, 2 * k + 1))
        mask = cv2.dilate(mask, kernel)

    area = float(np.count_nonzero(mask)) / float(w * h)
    info["area_frac"] = round(area, 4)
    if area < min_area_frac:
        log.warning("motion ROI covers only %.2f%% of the frame; ROI disabled", area * 100)
        return None, info
    info["enabled"] = True
    return mask, info


def masked_diff_score(previous_gray, gray, mask=None, min_blob_px=0, diff_thr=18):
    """Frame-difference motion score in [0, 1] restricted to `mask`.

    changed fraction + mean magnitude, both measured only inside the ROI.
    Blobs smaller than `min_blob_px` are dropped (leaves, birds, noise).
    """
    diff = cv2.absdiff(gray, previous_gray)
    changed = (diff > diff_thr).astype(np.uint8)

    if min_blob_px and min_blob_px > 1:
        n, labels, stats, _ = cv2.connectedComponentsWithStats(changed, connectivity=8)
        if n > 1:
            keep = np.zeros(n, dtype=np.uint8)
            keep[1:] = (stats[1:, cv2.CC_STAT_AREA] >= int(min_blob_px)).astype(np.uint8)
            changed = keep[labels]
        else:
            changed = np.zeros_like(changed)

    if mask is not None:
        inside = mask > 0
        denom = float(max(1, np.count_nonzero(inside)))
        frac = float(np.count_nonzero(changed[inside])) / denom
        mag = float(diff[inside].mean()) / 255.0 if denom > 1 else 0.0
        # magnitude only counts when something real (a blob) changed
        if frac == 0.0:
            mag = 0.0
    else:
        frac = float(changed.mean())
        mag = float(diff.mean()) / 255.0 if frac > 0 else 0.0
    return min(1.0, frac * 2.0 + mag)
