
from collections import Counter, deque

import numpy as np
from scipy.optimize import linear_sum_assignment

HISTORY_S = 120.0


def iou_matrix(a, b):
    if len(a) == 0 or len(b) == 0:
        return np.zeros((len(a), len(b)))
    a, b = np.asarray(a, float), np.asarray(b, float)
    ix1 = np.maximum(a[:, None, 0], b[None, :, 0])
    iy1 = np.maximum(a[:, None, 1], b[None, :, 1])
    ix2 = np.minimum(a[:, None, 2], b[None, :, 2])
    iy2 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(ix2 - ix1, 0, None) * np.clip(iy2 - iy1, 0, None)
    aa = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    ab = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return inter / (aa[:, None] + ab[None, :] - inter + 1e-9)


class Track:
    def __init__(self, tid, bbox, score, cls, ts):
        self.id = tid
        self.bbox = np.array(bbox, float)
        self.score = float(score)
        self.cls = int(cls)
        self.cls_votes = Counter({int(cls): 1})
        self.first_ts = self.last_ts = ts
        self.hits = 1
        self.vel = np.zeros(2)  # centre velocity, px/s

        # Existing history format is preserved for SlotManager.
        self.history = deque()  # (ts, cx, bottom_y, width)

        # New independent history for apparent-size direction detection.
        # Entries: (ts, width, height, area)
        self.size_history = deque()

        self.confirmed = False
        self._push(ts)

    def _push(self, ts):
        x1, y1, x2, y2 = self.bbox
        width = max(0.0, float(x2 - x1))
        height = max(0.0, float(y2 - y1))

        self.history.append(
            (ts, (x1 + x2) / 2, y2, width)
        )
        self.size_history.append(
            (ts, width, height, width * height)
        )

        while self.history and ts - self.history[0][0] > HISTORY_S:
            self.history.popleft()

        while self.size_history and ts - self.size_history[0][0] > HISTORY_S:
            self.size_history.popleft()

    def predict(self, ts):
        dt = min(max(ts - self.last_ts, 0.0), 1.0)
        sx, sy = self.vel * dt
        return self.bbox + np.array([sx, sy, sx, sy])

    def update(self, bbox, score, cls, ts):
        dt = max(ts - self.last_ts, 1e-3)
        old_c = np.array([
            (self.bbox[0] + self.bbox[2]) / 2,
            (self.bbox[1] + self.bbox[3]) / 2,
        ])
        new_c = np.array([
            (bbox[0] + bbox[2]) / 2,
            (bbox[1] + bbox[3]) / 2,
        ])
        self.vel = 0.6 * self.vel + 0.4 * (new_c - old_c) / dt
        self.bbox = np.array(bbox, float)
        self.score = float(score)
        self.cls = int(cls)
        self.cls_votes[int(cls)] += 1
        self.last_ts = ts
        self.hits += 1
        self._push(ts)

    @property
    def major_cls(self):
        return self.cls_votes.most_common(1)[0][0]


class ByteTracker:
    """ByteTrack-style two-stage association (high then low confidence
    detections) with constant-velocity prediction, IoU + Hungarian matching.
    Time-based (seconds), so it behaves the same at 3 fps or 10 fps."""

    def __init__(self, high_thr=0.45, low_thr=0.15, match_iou=0.25,
                 low_match_iou=0.15, max_age_s=2.0, min_hits=3):
        self.high_thr, self.low_thr = high_thr, low_thr
        self.match_iou, self.low_match_iou = match_iou, low_match_iou
        self.max_age_s, self.min_hits = max_age_s, min_hits
        self.tracks = []
        self._next = 1

    def _associate(self, preds, tidx, dets, thr):
        if not tidx or len(dets) == 0:
            return [], list(tidx), list(range(len(dets)))

        iou = iou_matrix(preds[tidx], dets[:, :4])

        # Make below-threshold matches prohibitively expensive.
        valid = iou >= thr
        cost = np.where(valid, 1.0 - iou, 1e6)

        rows, cols = linear_sum_assignment(cost)

        matches = []
        matched_tracks = set()
        matched_dets = set()

        for r, c in zip(rows, cols):
            if valid[r, c]:
                track_idx = tidx[r]
                matches.append((track_idx, c))
                matched_tracks.add(track_idx)
                matched_dets.add(c)

        return (
            matches,
            [t for t in tidx if t not in matched_tracks],
            [d for d in range(len(dets)) if d not in matched_dets],
        )

    def _associate_recovery(self, preds, tidx, dets):
        if not tidx or len(dets) == 0:
            return [], list(tidx), list(range(len(dets)))

        track_boxes = np.asarray(preds[tidx], dtype=float)
        det_boxes = np.asarray(dets[:, :4], dtype=float)

        tx = (track_boxes[:, 0] + track_boxes[:, 2]) / 2.0
        ty = (track_boxes[:, 1] + track_boxes[:, 3]) / 2.0
        dx = (det_boxes[:, 0] + det_boxes[:, 2]) / 2.0
        dy = (det_boxes[:, 1] + det_boxes[:, 3]) / 2.0

        tw = np.maximum(track_boxes[:, 2] - track_boxes[:, 0], 1.0)
        th = np.maximum(track_boxes[:, 3] - track_boxes[:, 1], 1.0)
        dw = np.maximum(det_boxes[:, 2] - det_boxes[:, 0], 1.0)
        dh = np.maximum(det_boxes[:, 3] - det_boxes[:, 1], 1.0)

        distance = np.sqrt(
            (tx[:, None] - dx[None, :]) ** 2
            + (ty[:, None] - dy[None, :]) ** 2
        )
        scale = np.maximum(
            np.sqrt(tw[:, None] ** 2 + th[:, None] ** 2), 1.0
        )
        normalized_distance = distance / scale

        width_ratio = np.minimum(
            tw[:, None], dw[None, :]
        ) / np.maximum(tw[:, None], dw[None, :])
        height_ratio = np.minimum(
            th[:, None], dh[None, :]
        ) / np.maximum(th[:, None], dh[None, :])
        size_similarity = np.minimum(width_ratio, height_ratio)

        valid = (
            (normalized_distance <= 0.60)
            & (size_similarity >= 0.55)
        )

        for row, track_idx in enumerate(tidx):
            track = self.tracks[track_idx]
            for col in range(len(dets)):
                if track.major_cls != int(dets[col, 5]):
                    valid[row, col] = False

        if not valid.any():
            return [], list(tidx), list(range(len(dets)))

        cost = normalized_distance + (1.0 - size_similarity)
        cost = np.where(valid, cost, 1e6)
        rows, cols = linear_sum_assignment(cost)

        matches = []
        used_tracks = set()
        used_dets = set()

        for row, col in zip(rows, cols):
            if not valid[row, col]:
                continue

            row_costs = np.sort(cost[row][valid[row]])
            col_costs = np.sort(cost[valid[:, col], col])

            if len(row_costs) > 1 and row_costs[1] - row_costs[0] < 0.12:
                continue
            if len(col_costs) > 1 and col_costs[1] - col_costs[0] < 0.12:
                continue

            track_idx = tidx[row]
            matches.append((track_idx, col))
            used_tracks.add(track_idx)
            used_dets.add(col)

        return (
            matches,
            [t for t in tidx if t not in used_tracks],
            [d for d in range(len(dets)) if d not in used_dets],
        )

    def update(self, dets, ts):
        dets = (
            np.zeros((0, 6))
            if dets is None or len(dets) == 0
            else np.asarray(dets, float)
        )
        high = dets[dets[:, 4] >= self.high_thr]
        low = dets[
            (dets[:, 4] >= self.low_thr)
            & (dets[:, 4] < self.high_thr)
        ]
        preds = (
            np.array([t.predict(ts) for t in self.tracks])
            if self.tracks else np.zeros((0, 4))
        )

        m1, un_t, un_d = self._associate(
            preds, list(range(len(self.tracks))), high, self.match_iou
        )
        # Conservative second-chance association before creating new IDs.
        un_d_original = list(un_d)
        recovery_matches, un_t, remaining_local_d = (
            self._associate_recovery(preds, un_t, high[un_d_original])
        )

        for ti, local_di in recovery_matches:
            d = high[un_d_original[local_di]]
            self.tracks[ti].update(d[:4], d[4], d[5], ts)

        un_d = [un_d_original[i] for i in remaining_local_d]
        for ti, di in m1:
            d = high[di]
            self.tracks[ti].update(d[:4], d[4], d[5], ts)

        recent = [
            t for t in un_t
            if ts - self.tracks[t].last_ts <= 1.0
        ]
        m2, _, _ = self._associate(
            preds, recent, low, self.low_match_iou
        )
        for ti, di in m2:
            d = low[di]
            self.tracks[ti].update(d[:4], d[4], d[5], ts)

        for di in un_d:
            d = high[di]
            self.tracks.append(
                Track(self._next, d[:4], d[4], d[5], ts)
            )
            self._next += 1

        for t in self.tracks:
            if not t.confirmed and t.hits >= self.min_hits:
                t.confirmed = True

        removed, alive = [], []
        for t in self.tracks:
            if ts - t.last_ts > self.max_age_s:
                if t.confirmed:
                    removed.append(t)
            else:
                alive.append(t)
        self.tracks = alive
        active = [
            t for t in self.tracks
            if t.last_ts == ts and t.confirmed
        ]
        return active, removed