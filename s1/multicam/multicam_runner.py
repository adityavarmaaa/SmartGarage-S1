
import cv2
import time
import threading
import numpy as np

from s1.tracker import ByteTracker


class GeometryFreeMultiCameraRunner:

    def __init__(
        self,
        detector,
        reid_manager,
        cameras,
        max_frames=None,
        queue_capacity=1,
        tracker_kwargs=None,
        max_batch_size=20,
        realtime_replay=False,
        batch_wait_ms=50,
        motion_threshold=8.0,
    ):
        self.detector = detector
        self.reid_manager = reid_manager
        self.cameras = dict(cameras)

        self.max_frames = max_frames
        self.max_batch_size = max(1, int(max_batch_size))
        self.batch_wait_s = max(0, batch_wait_ms) / 1000.0
        self.realtime_replay = bool(realtime_replay)
        self.motion_threshold = float(motion_threshold)

        # Exactly one latest-frame slot per camera.
        self.slots = {cam: None for cam in self.cameras}
        self.previous_gray = {cam: None for cam in self.cameras}

        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.done = set()
        self.threads = []

        self.fps = {}
        self.errors = {}
        self.last_served = {cam: 0.0 for cam in self.cameras}

        self.tracker_kwargs = tracker_kwargs or {
            "high_thr": 0.45,
            "low_thr": 0.15,
            "match_iou": 0.25,
            "low_match_iou": 0.15,
            "max_age_s": 2.0,
            "min_hits": 3,
        }

        self.trackers = {
            cam: ByteTracker(**self.tracker_kwargs)
            for cam in self.cameras
        }

        self.stats = {
            cam: {
                "captured": 0,
                "motion_frames": 0,
                "processed": 0,
                "detections": 0,
                "replaced_stale_frames": 0,
                "buffer_highwater": 0,
                "assignments": {},
                "max_wait_ms": 0.0,
            }
            for cam in self.cameras
        }

        self.batch_stats = {
            "calls": 0,
            "frames": 0,
            "sizes": {},
            "total_inference_s": 0.0,
            "max_inference_s": 0.0,
            "total_batch_wait_s": 0.0,
        }

    def _motion_score(self, camera_id, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (160, 90))

        previous = self.previous_gray[camera_id]
        self.previous_gray[camera_id] = gray

        if previous is None:
            return 0.0

        difference = cv2.absdiff(gray, previous)
        return float(np.mean(difference))

    def _capture(self, camera_id, source):
        cap = cv2.VideoCapture(source)

        if not cap.isOpened():
            with self.lock:
                self.errors[camera_id] = (
                    "Cannot open source: " + str(source)
                )
                self.done.add(camera_id)
            return

        fps = cap.get(cv2.CAP_PROP_FPS)
        if not fps or fps <= 0 or fps > 120:
            fps = 30.0

        self.fps[camera_id] = fps

        replay_start = time.monotonic()
        frame_idx = 0

        try:
            while not self.stop_event.is_set():

                if (
                    self.max_frames is not None
                    and frame_idx >= self.max_frames
                ):
                    break

                ok, frame = cap.read()
                if not ok:
                    break

                frame_idx += 1

                # Pace prerecorded videos like live cameras.
                if self.realtime_replay:
                    target = replay_start + frame_idx / fps
                    delay = target - time.monotonic()

                    if delay > 0:
                        self.stop_event.wait(delay)

                timestamp = frame_idx / fps
                motion_score = self._motion_score(camera_id, frame)

                with self.lock:
                    self.stats[camera_id]["captured"] += 1

                    if motion_score >= self.motion_threshold:
                        self.stats[camera_id]["motion_frames"] += 1

                    old = self.slots[camera_id]

                    if old is not None:
                        self.stats[camera_id][
                            "replaced_stale_frames"
                        ] += 1

                    self.slots[camera_id] = {
                        "frame": frame,
                        "timestamp": timestamp,
                        "captured_at": time.monotonic(),
                        "motion_score": motion_score,
                        "frame_idx": frame_idx,
                    }

                    self.stats[camera_id]["buffer_highwater"] = 1

        except Exception as exc:
            with self.lock:
                self.errors[camera_id] = repr(exc)

        finally:
            cap.release()
            with self.lock:
                self.done.add(camera_id)

    def _select_batch(self):
        now = time.monotonic()

        with self.lock:
            ready = []

            for camera_id, item in self.slots.items():
                if item is None:
                    continue

                age = now - item["captured_at"]
                wait_since_service = now - self.last_served[camera_id]

                # Waiting time is the primary scheduling factor.
                # Motion activity adds priority without starving
                # cameras that have less movement.
                motion_bonus = min(
                    item["motion_score"] / 255.0, 1.0
                ) * 0.10

                priority = wait_since_service + age + motion_bonus

                ready.append(
                    (priority, age, camera_id, item)
                )

            ready.sort(key=lambda x: x[0], reverse=True)

            selected = ready[:self.max_batch_size]

            batch = []

            for _, age, camera_id, item in selected:
                if self.slots[camera_id] is not item:
                    continue

                self.slots[camera_id] = None
                self.last_served[camera_id] = now

                self.stats[camera_id]["max_wait_ms"] = max(
                    self.stats[camera_id]["max_wait_ms"],
                    age * 1000.0,
                )

                batch.append((camera_id, item))

            return batch

    def _process_batch(self, batch):
        if not batch:
            return

        images = [item["frame"] for _, item in batch]

        start = time.perf_counter()
        detections = self.detector.detect_batch(images)
        inference_s = time.perf_counter() - start

        if detections is None:
            detections = [[] for _ in batch]

        if len(detections) != len(batch):
            raise RuntimeError(
                "YOLO batch output count does not match input count: "
                f"{len(detections)} inputs, {len(detections)} outputs"
            )

        self.batch_stats["calls"] += 1
        self.batch_stats["frames"] += len(batch)
        self.batch_stats["total_inference_s"] += inference_s
        self.batch_stats["max_inference_s"] = max(
            self.batch_stats["max_inference_s"],
            inference_s,
        )

        size = len(batch)
        self.batch_stats["sizes"][size] = (
            self.batch_stats["sizes"].get(size, 0) + 1
        )

        for (camera_id, item), dets in zip(batch, detections):
            timestamp = item["timestamp"]
            frame = item["frame"]

            active, removed = self.trackers[camera_id].update(
                dets, timestamp
            )

            # Each camera retains its own tracker state.
            assignments = self.reid_manager.update(
                camera_id,
                frame,
                active,
                timestamp,
            )

            with self.lock:
                self.stats[camera_id]["processed"] += 1
                self.stats[camera_id]["detections"] += len(dets)

                for local_id, global_id in assignments.items():
                    self.stats[camera_id]["assignments"][
                        int(local_id)
                    ] = global_id

    def run(self):
        start = time.monotonic()

        # One capture thread per configured camera.
        for camera_id, source in self.cameras.items():
            thread = threading.Thread(
                target=self._capture,
                args=(camera_id, source),
                daemon=True,
                name=f"capture-{camera_id}",
            )
            thread.start()
            self.threads.append(thread)

        try:
            while True:
                batch = self._select_batch()

                if batch:
                    self._process_batch(batch)
                    continue

                with self.lock:
                    all_done = len(self.done) == len(self.cameras)
                    all_empty = all(
                        value is None for value in self.slots.values()
                    )

                if all_done and all_empty:
                    break

                # Wait briefly for more cameras to provide frames.
                # Stop waiting immediately when the maximum batch fills.
                wait_start = time.monotonic()

                if self.batch_wait_s > 0:
                    deadline = wait_start + self.batch_wait_s

                    while time.monotonic() < deadline:
                        with self.lock:
                            ready_count = sum(
                                value is not None
                                for value in self.slots.values()
                            )

                            finished = (
                                len(self.done) == len(self.cameras)
                            )

                        if ready_count >= self.max_batch_size or finished:
                            break

                        time.sleep(0.002)

                    self.batch_stats["total_batch_wait_s"] += (
                        time.monotonic() - wait_start
                    )

        except Exception:
            self.stop_event.set()
            raise

        finally:
            self.stop_event.set()

            for thread in self.threads:
                thread.join(timeout=5)

        elapsed = time.monotonic() - start

        return {
            "cameras": self.stats,
            "batches": self.batch_stats,
            "registry": self.reid_manager.registry.summary(),
            "errors": self.errors,
            "elapsed_seconds": elapsed,
            "configured_max_batch_size": self.max_batch_size,
            "configured_batch_wait_ms": self.batch_wait_s * 1000,
        }
