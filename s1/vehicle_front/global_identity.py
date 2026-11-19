import numpy as np

class GlobalIdentityRegistry:
    def __init__(self, threshold=0.75, update_rate=0.15):
        self.threshold = threshold
        self.update_rate = update_rate
        self.identities = {}
        self.local_to_global = {}
        self.counter = 0

    def reserve_existing_ids(self, global_ids):
        """Reserve IDs already issued by the ENTRY session."""
        maximum = self.counter
        for value in global_ids or []:
            text = str(value)
            if text.startswith("GID-") and text[4:].isdigit():
                maximum = max(maximum, int(text[4:]))
        self.counter = maximum
        return self.counter


    @staticmethod
    def normalize(embedding):
        x = np.asarray(embedding, dtype=np.float32).reshape(-1)
        n = np.linalg.norm(x)
        if not np.isfinite(n) or n < 1e-8:
            raise ValueError("Invalid embedding")
        return x / n

    def assign(self, camera_id, local_id, embedding, timestamp):
        camera_id = str(camera_id)
        local_id = int(local_id)
        timestamp = float(timestamp)
        embedding = self.normalize(embedding)

        key = (camera_id, local_id)

        if key in self.local_to_global:
            gid = self.local_to_global[key]
            item = self.identities[gid]
            item["prototype"] = self.normalize(
                (1-self.update_rate)*item["prototype"]
                + self.update_rate*embedding
            )
            item["observations"] += 1
            item["last_seen"][camera_id] = timestamp
            return {"global_id": gid, "status": "EXISTING"}

        candidates = []

        for gid, item in self.identities.items():
            previous = item["last_seen"].get(camera_id)
            if previous is not None and timestamp - previous <= 2.0:
                continue

            score = float(np.dot(item["prototype"], embedding))
            candidates.append((score, gid))

        candidates.sort(reverse=True)

        if not candidates or candidates[0][0] < self.threshold:
            self.counter += 1
            gid = f"GID-{self.counter:05d}"
            self.identities[gid] = {
                "prototype": embedding.copy(),
                "observations": 1,
                "cameras": {camera_id},
                "last_seen": {camera_id: timestamp}
            }
            self.local_to_global[key] = gid
            return {"global_id": gid, "status": "NEW"}

        score, gid = candidates[0]

        if len(candidates) > 1 and score - candidates[1][0] < 0.04:
            return {"global_id": None, "status": "AMBIGUOUS"}

        item = self.identities[gid]
        item["prototype"] = self.normalize(
            0.85*item["prototype"] + 0.15*embedding
        )
        item["observations"] += 1
        item["cameras"].add(camera_id)
        item["last_seen"][camera_id] = timestamp
        self.local_to_global[key] = gid

        return {"global_id": gid, "status": "MATCHED"}

    def summary(self):
        return [
            {
                "global_id": gid,
                "observations": item["observations"],
                "cameras": sorted(item["cameras"])
            }
            for gid, item in self.identities.items()
        ]
