"""Reference-frame camera motion estimation for virtual parking geometry.

The returned affine matrix maps points in the CURRENT source frame into the
REFERENCE source frame. Detection and tracking remain in current-frame pixels.
"""
from __future__ import annotations

import csv
import math
import os
import time

import cv2
import numpy as np


def transform_points(points, matrix):
    """Apply a 2x3 affine transform to an iterable of (x, y) points."""
    pts = np.asarray(points, dtype=np.float32).reshape(-1, 1, 2)
    if len(pts) == 0:
        return np.empty((0, 2), dtype=np.float32)
    return cv2.transform(pts, np.asarray(matrix, dtype=np.float32)).reshape(-1, 2)


def transform_bbox(bbox, matrix):
    """Transform a bbox's four corners and return its reference-space AABB."""
    x1, y1, x2, y2 = map(float, bbox)
    pts = transform_points([(x1, y1), (x2, y1), (x2, y2), (x1, y2)], matrix)
    return (float(pts[:, 0].min()), float(pts[:, 1].min()),
            float(pts[:, 0].max()), float(pts[:, 1].max()))


class CameraMotionEstimator:
    """Estimate current-to-reference motion with ORB/RANSAC and ECC fallback.

    Low-quality estimates are rejected and the last good transform is held.
    Diagnostics are appended to CSV when ``diagnostics_path`` is supplied.
    """
    def __init__(self, diagnostics_path=None, max_dimension=800, min_inliers=8,
                 max_translation_fraction=0.35, max_rotation_deg=20.0,
                 min_scale=0.65, max_scale=1.50, smoothing=0.60):
        self.max_dimension = int(max_dimension)
        self.min_inliers = int(min_inliers)
        self.max_translation_fraction = float(max_translation_fraction)
        self.max_rotation_deg = float(max_rotation_deg)
        self.min_scale, self.max_scale = float(min_scale), float(max_scale)
        self.smoothing = float(smoothing)
        self.orb = cv2.ORB_create(nfeatures=1800, scaleFactor=1.2, nlevels=8)
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        self.reference_gray = None
        self.reference_size = None
        self.reference_kp = None
        self.reference_desc = None
        # Previous usable feature frame and its mapping into geometry/reference
        # coordinates. This is a fallback when a pan/tilt/zoom loses direct
        # overlap with the very first frame.
        self.previous_kp = None
        self.previous_desc = None
        self.previous_to_reference = np.array(
            [[1., 0., 0.], [0., 1., 0.]], dtype=np.float32
        )
        self.last_good = np.array([[1., 0., 0.], [0., 1., 0.]], dtype=np.float32)
        self.quality = "reference"
        self.inliers = 0
        self._csv_path = diagnostics_path
        self._csv_ready = False

    def _gray(self, image):
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        h, w = gray.shape[:2]
        scale = min(1.0, float(self.max_dimension) / max(h, w))
        if scale < 1.0:
            gray = cv2.resize(gray, (max(1, round(w * scale)), max(1, round(h * scale))),
                              interpolation=cv2.INTER_AREA)
        return gray, scale

    def _log(self, frame_id, matrix, inliers, method, accepted):
        if not self._csv_path:
            return
        parent = os.path.dirname(self._csv_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        new_file = not os.path.exists(self._csv_path) or os.path.getsize(self._csv_path) == 0
        a = np.asarray(matrix, dtype=float)
        scale = math.sqrt(max(0.0, a[0, 0] ** 2 + a[1, 0] ** 2))
        rotation = math.degrees(math.atan2(a[1, 0], a[0, 0]))
        with open(self._csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(["time", "frame_id", "method", "accepted", "inliers",
                                 "tx_px", "ty_px", "rotation_deg", "scale", "quality"])
            writer.writerow([f"{time.time():.3f}", frame_id, method, int(accepted), int(inliers),
                             f"{a[0, 2]:.3f}", f"{a[1, 2]:.3f}", f"{rotation:.4f}",
                             f"{scale:.6f}", self.quality])
    def _log(
        self,
        frame_id,
        matrix,
        inliers,
        method,
        accepted,
        rejection_reason="",
        ecc_score=None,
        candidate_matrix=None,
    ):
        if not self._csv_path:
            return

        parent = os.path.dirname(self._csv_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        new_file = (
            not os.path.exists(self._csv_path)
            or os.path.getsize(self._csv_path) == 0
        )

        # Log the candidate separately from the last accepted transform.
        candidate = (
            np.asarray(candidate_matrix, dtype=float)
            if candidate_matrix is not None
            else None
        )
        held = np.asarray(matrix, dtype=float)

        def metrics(a):
            if a is None or a.shape != (2, 3) or not np.isfinite(a).all():
                return ["", "", "", ""]
            scale = math.hypot(a[0, 0], a[1, 0])
            rotation = math.degrees(
                math.atan2(a[1, 0], a[0, 0])
            )
            return [
                f"{a[0, 2]:.3f}",
                f"{a[1, 2]:.3f}",
                f"{rotation:.4f}",
                f"{scale:.6f}",
            ]

        with open(self._csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow([
                    "time", "frame_id", "method", "accepted", "inliers",
                    "candidate_tx_px", "candidate_ty_px",
                    "candidate_rotation_deg", "candidate_scale",
                    "rejection_reason", "ecc_score",
                    "held_tx_px", "held_ty_px",
                    "held_rotation_deg", "held_scale", "quality",
                ])

            writer.writerow([
                f"{time.time():.3f}",
                frame_id,
                method,
                int(accepted),
                int(inliers),
                *metrics(candidate),
                rejection_reason,
                "" if ecc_score is None else f"{ecc_score:.5f}",
                *metrics(held),
                self.quality,
            ])


    def _to_source_matrix(self, small_matrix, scale):
        # S^-1 * A_small * S converts a transform between resized images to
        # the corresponding transform in original source-frame coordinates.
        s_inv = np.array([[1.0 / scale, 0, 0], [0, 1.0 / scale, 0], [0, 0, 1]])
        s = np.array([[scale, 0, 0], [0, scale, 0], [0, 0, 1]])
        return (s_inv @ np.vstack([small_matrix, [0, 0, 1]]) @ s)[:2].astype(np.float32)

    
    def _rejection_reason(self, matrix, inliers, gray_shape):
        """Explain why a candidate transform is rejected."""
        if matrix is None:
            return "no_candidate"

        if inliers < self.min_inliers:
            return "insufficient_inliers"

        a = np.asarray(matrix, dtype=float)
        if a.shape != (2, 3) or not np.isfinite(a).all():
            return "invalid_matrix"

        scale = math.hypot(a[0, 0], a[1, 0])
        rotation = abs(math.degrees(math.atan2(a[1, 0], a[0, 0])))

        if self.reference_size:
            w, h = self.reference_size
        else:
            h, w = gray_shape

        translation = math.hypot(a[0, 2], a[1, 2])

        if scale < self.min_scale or scale > self.max_scale:
            return "scale_out_of_range"

        if rotation > self.max_rotation_deg:
            return "rotation_out_of_range"

        if translation > self.max_translation_fraction * math.hypot(w, h):
            return "translation_out_of_range"

        return "accepted"

    def _acceptable(self, matrix, inliers, gray_shape):
        return self._rejection_reason(matrix, inliers, gray_shape) == "accepted"


    @staticmethod
    def _homogeneous(matrix):
        return np.vstack([np.asarray(matrix, dtype=np.float32),
                          np.array([0.0, 0.0, 1.0], dtype=np.float32)])

    @staticmethod
    def _affine_from_matches(source_kp, source_desc, destination_kp,
                             destination_desc, matcher, scale, min_inliers):
        """Estimate source-image -> destination-image affine transform."""
        if (source_desc is None or destination_desc is None
                or len(source_desc) < min_inliers
                or len(destination_desc) < min_inliers):
            return None, 0
        try:
            pairs = matcher.knnMatch(source_desc, destination_desc, k=2)
        except cv2.error:
            return None, 0
        good = [m for pair in pairs if len(pair) == 2
                for m, n in [pair] if m.distance < 0.75 * n.distance]
        if len(good) < min_inliers:
            return None, 0
        src = np.float32([source_kp[m.queryIdx].pt for m in good])
        dst = np.float32([destination_kp[m.trainIdx].pt for m in good])
        small, inlier_mask = cv2.estimateAffinePartial2D(
            src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.0,
            maxIters=2500, confidence=0.99, refineIters=10)
        inliers = int(inlier_mask.sum()) if inlier_mask is not None else 0
        if small is None or inliers < min_inliers:
            return None, inliers
        return CameraMotionEstimator._matrix_to_source(small, scale), inliers

    @staticmethod
    def _matrix_to_source(small_matrix, scale):
        # S^-1 * A_small * S converts resized-image affine coordinates back
        # to original source-frame pixels.
        s_inv = np.array([[1.0 / scale, 0, 0], [0, 1.0 / scale, 0], [0, 0, 1]])
        s = np.array([[scale, 0, 0], [0, scale, 0], [0, 0, 1]])
        return (s_inv @ CameraMotionEstimator._homogeneous(small_matrix) @ s)[:2].astype(np.float32)

    @staticmethod
    def _mask_boxes(gray_shape, moving_boxes, scale):
        h, w = gray_shape
        mask = np.full((h, w), 255, dtype=np.uint8)
        for box in moving_boxes or ():
            x1, y1, x2, y2 = map(float, box)
            x1 = int(max(0, min(w, round(x1 * scale))))
            x2 = int(max(0, min(w, round(x2 * scale))))
            y1 = int(max(0, min(h, round(y1 * scale))))
            y2 = int(max(0, min(h, round(y2 * scale))))
            if x2 > x1 and y2 > y1:
                cv2.rectangle(mask, (x1, y1), (x2, y2), 0, -1)
        return mask

    @staticmethod
    def _matrix_metrics(matrix):
        a = np.asarray(matrix, dtype=float)
        scale = math.hypot(a[0, 0], a[1, 0])
        rotation = math.degrees(math.atan2(a[1, 0], a[0, 0]))
        translation = math.hypot(a[0, 2], a[1, 2])
        return scale, rotation, translation

    def _smooth_to(self, candidate, alpha=None):
        old = self.last_good.astype(float)
        new = np.asarray(candidate, dtype=float)
        # Angles must stay in radians inside sin/cos; _matrix_metrics is for
        # human-readable diagnostic degrees and must not be reused here.
        old_scale = math.hypot(old[0, 0], old[1, 0])
        new_scale = math.hypot(new[0, 0], new[1, 0])
        old_angle = math.atan2(old[1, 0], old[0, 0])
        new_angle = math.atan2(new[1, 0], new[0, 0])
        a = self.smoothing if alpha is None else float(alpha)
        scale = (1.0 - a) * old_scale + a * new_scale
        angle = old_angle + a * math.atan2(
            math.sin(new_angle - old_angle), math.cos(new_angle - old_angle)
        )
        tx = (1.0 - a) * old[0, 2] + a * new[0, 2]
        ty = (1.0 - a) * old[1, 2] + a * new[1, 2]
        self.last_good = np.array([
            [scale * math.cos(angle), -scale * math.sin(angle), tx],
            [scale * math.sin(angle),  scale * math.cos(angle), ty],
        ], dtype=np.float32)

    def update(self, image, moving_boxes=(), frame_id=None):
        """Return a validated current-frame -> calibrated-reference transform.

        First try matching to the reference frame, which bounds accumulated
        drift. If a moving camera loses enough overlap for that match, estimate
        current -> previous and compose it with the previous frame's validated
        reference transform. A weak/textureless frame never resets the transform.
        """
        gray, scale = self._gray(image)
        mask = self._mask_boxes(gray.shape, moving_boxes, scale)
        kp, desc = self.orb.detectAndCompute(gray, mask)

        if self.reference_gray is None:
            self.reference_gray = gray
            self.reference_size = image.shape[1], image.shape[0]
            # Exclude detected vehicles even in the very first frame. Otherwise
            # their features can drag the reference as they move.
            self.reference_kp, self.reference_desc = kp, desc
            self.previous_kp, self.previous_desc = kp, desc
            self.previous_to_reference = self.last_good.copy()
            self.quality, self.inliers = "reference", 0
            self._log(frame_id, self.last_good, 0, "reference", True,
                      rejection_reason="reference_frame")
            return self.last_good.copy()

        candidate, inliers = self._affine_from_matches(
            kp, desc, self.reference_kp, self.reference_desc,
            self.matcher, scale, self.min_inliers,
        )
        method = "orb_ransac"
        rejection_reason = self._rejection_reason(candidate, inliers, gray.shape)
        accepted = rejection_reason == "accepted"
        ecc_score = None

        # Fallback: chain a local transform from this frame to the last feature
        # frame with a known reference pose. The local motion is checked against
        # conservative per-step limits before it is composed.
        if not accepted and self.previous_desc is not None:
            incremental, local_inliers = self._affine_from_matches(
                kp, desc, self.previous_kp, self.previous_desc,
                self.matcher, scale, self.min_inliers,
            )
            if incremental is not None and local_inliers >= self.min_inliers:
                inc_scale, inc_rotation, inc_translation = self._matrix_metrics(incremental)
                diagonal = math.hypot(image.shape[1], image.shape[0])
                local_ok = (
                    np.isfinite(incremental).all()
                    and self.min_scale <= inc_scale <= self.max_scale
                    and abs(inc_rotation) <= self.max_rotation_deg
                    and inc_translation <= 0.30 * diagonal
                )
                if local_ok:
                    candidate = (
                        self._homogeneous(self.previous_to_reference)
                        @ self._homogeneous(incremental)
                    )[:2].astype(np.float32)
                    inliers = local_inliers
                    method = "orb_prev_ransac"
                    rejection_reason = "accepted_incremental"
                    accepted = True
                else:
                    rejection_reason = "incremental_transform_out_of_range"

        # Final fallback: ECC is useful for small motion but is intentionally
        # accepted only with a strong correlation score.
        if not accepted and desc is not None and self.reference_desc is not None:
            try:
                warp = np.eye(2, 3, dtype=np.float32)
                criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 35, 1e-5)
                score, warp = cv2.findTransformECC(
                    self.reference_gray, gray, warp, cv2.MOTION_AFFINE,
                    criteria, None, 1
                )
                ecc_score = float(score)
                candidate = self._matrix_to_source(
                    cv2.invertAffineTransform(warp), scale
                )
                rejection_reason = self._rejection_reason(
                    candidate, self.min_inliers, gray.shape
                )
                if score < 0.75:
                    rejection_reason = "ecc_score_too_low"
                accepted = rejection_reason == "accepted"
                if accepted:
                    method = "ecc_fallback"
                    inliers = 0
            except cv2.error:
                candidate, accepted = None, False
                rejection_reason = "ecc_error"

        if accepted and candidate is not None:
            # A reference-frame estimate is already anchored to the calibrated
            # first frame. Blending it with last frame's pose creates visible
            # lag between the camera and the slot outlines. Apply those absolute
            # estimates directly; lightly smooth only chained incremental ones.
            if method in {"orb_ransac", "ecc_fallback"}:
                self.last_good = np.asarray(candidate, dtype=np.float32).copy()
            else:
                self._smooth_to(candidate, alpha=max(self.smoothing, 0.90))
            self.quality = {
                "orb_ransac": "good",
                "orb_prev_ransac": "incremental",
                "ecc_fallback": "ecc",
            }.get(method, "good")
        else:
            self.quality = "held_last_good"

        self.inliers = inliers
        # Only advance the incremental anchor when this frame's pose was
        # accepted. Associating a failed frame's descriptors with the held pose
        # would silently lose the camera movement between those two frames.
        if accepted and desc is not None and len(desc) >= self.min_inliers:
            self.previous_kp, self.previous_desc = kp, desc
            self.previous_to_reference = self.last_good.copy()

        self._log(
            frame_id, self.last_good, inliers, method, accepted,
            rejection_reason=rejection_reason, ecc_score=ecc_score,
            candidate_matrix=candidate,
        )
        return self.last_good.copy()

