
import time
from typing import Dict, Iterable, Optional

import numpy as np

from vehicle_intelligence.reid import OSNetEmbedder
from vehicle_intelligence.global_identity import GlobalIdentityRegistry


class GlobalReIDManager:
    """Connect confirmed local ByteTrack tracks to global identities."""

    def __init__(
        self,
        model_path: str,
        similarity_threshold: float = 0.75,
        embedding_interval_s: float = 0.5,
        update_rate: float = 0.15,
    ):
        self.embedder = OSNetEmbedder(model_path)
        self.registry = GlobalIdentityRegistry(
            threshold=similarity_threshold,
            update_rate=update_rate,
        )
        self.embedding_interval_s = embedding_interval_s
        self.last_embedding_at = {}
        self.local_to_global = {}

    @staticmethod
    def _crop(frame, bbox):
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = map(int, bbox)
        x1, x2 = max(0, x1), min(w, x2)
        y1, y2 = max(0, y1), min(h, y2)

        if x2 <= x1 or y2 <= y1:
            return None

        crop = frame[y1:y2, x1:x2]
        if crop.size == 0 or crop.shape[0] < 16 or crop.shape[1] < 16:
            return None
        return crop

    def update(self, camera_id: str, frame, tracks: Iterable, timestamp: Optional[float] = None):
        now = float(timestamp if timestamp is not None else time.time())
        result = {}

        for track in tracks:
            if not getattr(track, "confirmed", False):
                continue

            local_id = int(track.id)
            key = (str(camera_id), local_id)

            if key in self.local_to_global:
                result[local_id] = self.local_to_global[key]

            last = self.last_embedding_at.get(key, -1e18)
            if now - last < self.embedding_interval_s:
                continue

            crop = self._crop(frame, track.bbox)
            if crop is None:
                continue

            embedding = self.embedder.encode(crop)
            if embedding is None:
                continue

            embedding = np.asarray(embedding, dtype=np.float32).reshape(-1)
            if embedding.size != 512 or not np.all(np.isfinite(embedding)):
                continue

            assignment = self.registry.assign(
                camera_id=str(camera_id),
                local_id=local_id,
                embedding=embedding,
                timestamp=now,
            )

            gid = assignment.get("global_id")
            if gid is not None:
                self.local_to_global[key] = gid
                result[local_id] = gid
                self.last_embedding_at[key] = now

        return result

    def summary(self):
        return self.registry.summary()
