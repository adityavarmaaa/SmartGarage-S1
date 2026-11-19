import numpy as np

from s1.common.errors import Diagnostic, ProcessingError


def drop_contained_boxes(dets, frac=0.85, iou_thr=0.75):
    """Suppress likely duplicate boxes without discarding distinct vehicles."""
    dets = np.asarray(dets)
    if len(dets) < 2:
        return dets

    areas = (
        np.clip(dets[:, 2] - dets[:, 0], 0, None)
        * np.clip(dets[:, 3] - dets[:, 1], 0, None)
    )
    order = np.argsort(-dets[:, 4], kind="stable")
    kept = []

    for i in order:
        if areas[i] <= 0:
            continue

        duplicate = False
        for j in kept:
            if areas[j] <= 0:
                continue

            ix1 = max(dets[i, 0], dets[j, 0])
            iy1 = max(dets[i, 1], dets[j, 1])
            ix2 = min(dets[i, 2], dets[j, 2])
            iy2 = min(dets[i, 3], dets[j, 3])
            inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)

            if inter <= 0:
                continue

            union = areas[i] + areas[j] - inter
            iou = inter / max(union, 1e-9)
            same_class = dets[i, 5] == dets[j, 5]

            # Different classes are suppressed only when their boxes are
            # almost identical; nearby vehicles can legitimately overlap.
            if not same_class:
                if iou >= 0.90:
                    duplicate = True
                    break
                continue

            overlap_smaller = inter / max(min(areas[i], areas[j]), 1e-9)

            # High IoU indicates a duplicate regardless of confidence gap.
            if iou >= iou_thr:
                duplicate = True
                break

            # A contained box with a much lower score may be another vehicle,
            # so do not discard it solely because it lies inside a larger box.
            confidence_gap = abs(float(dets[i, 4]) - float(dets[j, 4]))
            if overlap_smaller >= frac and confidence_gap <= 0.08:
                duplicate = True
                break

        if not duplicate:
            kept.append(i)

    return dets[np.asarray(kept, dtype=int)]


class YoloDetector:
    CONTAINED_FRAC = 0.85
    DUPLICATE_IOU = 0.75

    """YOLO11 detector with the existing (N, 6) pipeline contract."""

    def __init__(
        self,
        model_path="yolo11n.pt",
        input_size=640,
        score_thr=0.25,
        nms_thr=0.45,
        classes=None,
        threads=2,
        device="cpu",
    ):
        self.size = input_size
        self.score_thr = score_thr
        self.nms_thr = nms_thr
        # Smart parking currently processes cars only (COCO class ID 2).
        # Enforce this even if an older config lists other vehicle classes.
        self.allowed = [2]
        self.device = device
        try:
            from ultralytics import YOLO
            self.model = YOLO(model_path)
            if str(self.device).startswith("cuda"):
                self.model.to(self.device)
            self.names = self.model.names
        except Exception as exc:
            raise ProcessingError(Diagnostic(
                "YOLO_MODEL_LOAD_FAILED", "VEHICLE_DETECTION", "YOLO", "initialize",
                "initialization", "Could not load the vehicle detection model.",
                False, "failed", "Stopped initialization.",
                context={"model_path": str(model_path), "device": str(device)},
            )) from exc

    def detect(self, img):
        result = self.model.predict(
            source=img,
            imgsz=self.size,
            conf=self.score_thr,
            iou=self.nms_thr,
            device=self.device,
            classes=self.allowed,
            verbose=False,
        )[0]

        if result.boxes is None or len(result.boxes) == 0:
            return np.zeros((0, 6), dtype=np.float32)

        xyxy = result.boxes.xyxy.cpu().numpy()
        conf = result.boxes.conf.cpu().numpy().reshape(-1, 1)
        cls = result.boxes.cls.cpu().numpy().reshape(-1, 1)
        out = np.concatenate((xyxy, conf, cls), axis=1).astype(np.float32)

        return drop_contained_boxes(
            out,
            frac=self.CONTAINED_FRAC,
            iou_thr=self.DUPLICATE_IOU,
        )


    def detect_batch(self, images):
        """Run shared YOLO inference on multiple images.

        Returns one (N, 6) float32 detection array per input image,
        preserving input order and the detect() output contract.
        """
        if images is None:
            raise ValueError("images cannot be None")

        images = list(images)
        if not images:
            return []

        results = self.model.predict(
            source=images,
            imgsz=self.size,
            conf=self.score_thr,
            iou=self.nms_thr,
            device=self.device,
            classes=self.allowed,
            verbose=False,
        )

        outputs = []
        for result in results:
            if result.boxes is None or len(result.boxes) == 0:
                output = np.zeros((0, 6), dtype=np.float32)
            else:
                xyxy = result.boxes.xyxy.cpu().numpy()
                conf = result.boxes.conf.cpu().numpy().reshape(-1, 1)
                cls = result.boxes.cls.cpu().numpy().reshape(-1, 1)

                output = np.concatenate(
                    (xyxy, conf, cls), axis=1
                ).astype(np.float32)

                output = drop_contained_boxes(
                    output,
                    frac=self.CONTAINED_FRAC,
                    iou_thr=self.DUPLICATE_IOU,
                )

            outputs.append(output)

        return outputs
