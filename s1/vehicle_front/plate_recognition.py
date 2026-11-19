
import logging
from s1.common.errors import Diagnostic
import queue
import re
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("plates")

STATE_CODES = set(
    "AN AP AR AS BR CH CG DD DL DN GA GJ HP HR JH JK KA KL LA LD MH ML MN MP MZ "
    "NL OD OR PB PY RJ SK TG TN TR TS UA UK UP WB".split()
)

_TO_DIGIT = {
    "O": "0", "Q": "0", "D": "0", "I": "1", "L": "1",
    "Z": "2", "S": "5", "B": "8", "G": "6", "T": "7",
}

_TO_LETTER = {
    "0": "O", "1": "I", "2": "Z", "5": "S",
    "8": "B", "6": "G", "4": "A", "7": "T",
}

_STD = re.compile(r"^[A-Z]{2}\d{2}[A-Z]{0,3}\d{4}$")
_STD1 = re.compile(r"^[A-Z]{2}\d[A-Z]{1,3}\d{4}$")
_BH = re.compile(r"^\d{2}BH\d{4}[A-Z]{1,2}$")


def normalize(text):
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def _apply(t, types):
    out = []
    for ch, ty in zip(t, types):
        if ty == "D":
            out.append(ch if ch.isdigit() else _TO_DIGIT.get(ch, ch))
        else:
            out.append(ch if ch.isalpha() else _TO_LETTER.get(ch, ch))
    return "".join(out)


def fix_indian(text):
    """Normalize and validate an Indian registration using positional rules."""
    t = normalize(text)
    n = len(t)

    if n in (9, 10) and t[2:4] == "BH":
        f = _apply(t, "DDLLDDDD" + "L" * (n - 8))
        if _BH.fullmatch(f):
            return f, True

    best = None

    for r in (2, 1):
        s = n - 6 - r

        if not 1 <= s <= 3:
            continue

        f = _apply(t, "LL" + "D" * r + "L" * s + "DDDD")
        pattern = _STD1 if r == 1 else _STD

        if pattern.fullmatch(f):
            if f[:2] in STATE_CODES and (r == 2 or f[:2] == "DL"):
                substitutions = sum(a != b for a, b in zip(t, f))

                if best is None or (substitutions, -r) < best[0]:
                    best = ((substitutions, -r), f)

    if best:
        return best[1], True

    return t, False


@dataclass
class Consensus:
    text: str
    conf: float
    n_reads: int
    valid: bool


class PlateVoter:
    """Collect exact normalized reads; never synthesize a plate."""

    def __init__(self, max_reads=60):
        self.reads = []
        self.max_reads = max_reads
        self._observation_ids = set()

    def add(self, raw_text, conf, observation_id=None):
        if observation_id is not None:
            if observation_id in self._observation_ids:
                return False
            self._observation_ids.add(observation_id)

        fixed, valid = fix_indian(raw_text)

        if len(fixed) < 6:
            return False

        if valid and fixed[2:4] != "BH":
            if len(fixed) not in (9, 10):
                return False

            state = fixed[:2]
            body = fixed[2:-4]
            suffix = fixed[-4:]

            if state not in STATE_CODES or not suffix.isdigit():
                return False

            district_len = 0

            while (
                district_len < min(2, len(body))
                and body[district_len].isdigit()
            ):
                district_len += 1

            series = body[district_len:]

            if (
                district_len not in (1, 2)
                or not (1 <= len(series) <= 3)
                or not series.isalpha()
            ):
                return False

        self.reads.append((fixed, float(conf), valid))

        if len(self.reads) > self.max_reads:
            self.reads.pop(0)

        return True

    def consensus(self):
        if not self.reads:
            return None

        valid_reads = [r for r in self.reads if r[2]]
        pool = valid_reads if valid_reads else self.reads

        grouped = {}

        for text, conf, valid in pool:
            item = grouped.setdefault(
                text,
                {"confs": [], "valid": valid},
            )
            item["confs"].append(float(conf))

        text, item = max(
            grouped.items(),
            key=lambda pair: (
                sum(pair[1]["confs"]),
                len(pair[1]["confs"]),
                pair[0],
            ),
        )

        confs = item["confs"]

        return Consensus(
            text=text,
            conf=sum(confs) / len(confs),
            n_reads=len(confs),
            valid=bool(item["valid"]),
        )

    def is_final(self, confirm_reads, confirm_conf):
        by_text = {}

        for text, conf, valid in self.reads:
            if valid:
                by_text.setdefault(text, []).append(float(conf))

        eligible = [
            (plate, confs)
            for plate, confs in by_text.items()
            if len(confs) >= confirm_reads
            and sum(confs) / len(confs) >= confirm_conf
        ]

        return len(eligible) == 1


def crop_quality(
    img_bgr,
    min_brightness=8.0,
    max_brightness=247.0,
    min_contrast=5.0,
    min_laplacian_var=2.0,
):
    """Conservative image-quality rejection gate."""

    import cv2
    import numpy as np

    if img_bgr is None or not getattr(img_bgr, "size", 0):
        return False, {"reason": "empty"}

    if (
        len(img_bgr.shape) < 2
        or img_bgr.shape[0] < 16
        or img_bgr.shape[1] < 16
    ):
        return False, {"reason": "too_small"}

    gray = (
        cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        if len(img_bgr.shape) == 3
        else img_bgr
    )

    brightness = float(np.mean(gray))
    contrast = float(np.std(gray))
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())

    metrics = {
        "brightness": round(brightness, 2),
        "contrast": round(contrast, 2),
        "laplacian_var": round(sharpness, 2),
    }

    if brightness < min_brightness or brightness > max_brightness:
        metrics["reason"] = "brightness"
        return False, metrics

    if contrast < min_contrast:
        metrics["reason"] = "contrast"
        return False, metrics

    if sharpness < min_laplacian_var:
        metrics["reason"] = "blur"
        return False, metrics

    return True, metrics


@dataclass
class PlateRead:
    text: str
    conf: float
    det_conf: float
    bbox: tuple


class PlateReader:
    """Fast ALPR with a targeted enhanced lower-crop retry."""

    def __init__(
        self,
        detector_model,
        ocr_model,
        min_ocr_conf=0.50,
        min_detector_conf=0.25,
    ):
        from fast_alpr import ALPR

        self.alpr = ALPR(
            detector_model=detector_model,
            ocr_model=ocr_model,
        )

        self.min_ocr_conf = float(min_ocr_conf)
        self.min_detector_conf = float(min_detector_conf)

    @staticmethod
    def _mean_conf(value):
        if value is None:
            return 0.0

        if hasattr(value, "tolist"):
            value = value.tolist()

        if isinstance(value, (list, tuple)):
            return sum(float(x) for x in value) / max(1, len(value))

        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _predict_variant(
        self,
        image,
        offset_x=0.0,
        offset_y=0.0,
        resize_scale=1.0,
        variant="original",
    ):
        predictions = self.alpr.predict(image)

        out = []
        detection_count = 0
        text_count = 0

        for result in predictions or []:
            detection = getattr(result, "detection", None)

            if (
                detection is None
                or getattr(detection, "bounding_box", None) is None
            ):
                continue

            detection_count += 1

            ocr = getattr(result, "ocr", None)
            text = str(getattr(ocr, "text", "") or "").strip()

            if not text:
                continue

            text_count += 1

            conf = self._mean_conf(
                getattr(ocr, "confidence", 0.0)
            )

            det_conf = self._mean_conf(
                getattr(detection, "confidence", 0.0)
            )

            # Privacy-safe diagnostics: no plate characters are logged.
            fixed_text, format_valid = fix_indian(text)

            log.info(
                "ALPR_READ variant=%s raw_len=%d normalized_len=%d "
                "format_valid=%s ocr_conf=%.3f detector_conf=%.3f",
                variant,
                len(normalize(text)),
                len(fixed_text),
                bool(format_valid),
                conf,
                det_conf,
            )

            bb = detection.bounding_box

            # Map processed variant coordinates back to original crop.
            x1 = float(bb.x1) / resize_scale + offset_x
            y1 = float(bb.y1) / resize_scale + offset_y
            x2 = float(bb.x2) / resize_scale + offset_x
            y2 = float(bb.y2) / resize_scale + offset_y

            out.append(
                PlateRead(
                    text=text,
                    conf=conf,
                    det_conf=det_conf,
                    bbox=(x1, y1, x2, y2),
                )
            )

        log.debug(
            "ALPR_VARIANT name=%s input=%sx%s "
            "detections=%d text_reads=%d",
            variant,
            image.shape[1],
            image.shape[0],
            detection_count,
            text_count,
        )

        return out, detection_count, text_count

    @staticmethod
    def _enhanced_lower_crop(img_bgr):
        """Enhance the lower vehicle region and return scale/offset."""

        import cv2
        import numpy as np

        h, w = img_bgr.shape[:2]

        y0 = int(round(h * 0.20)) if h >= 50 else 0
        roi = img_bgr[y0:h, :]

        if roi.size == 0:
            return img_bgr, 0.0, 0.0, 1.0

        lab = cv2.cvtColor(roi, cv2.COLOR_BGR2LAB)
        lightness, a, b = cv2.split(lab)

        lightness = cv2.createCLAHE(
            clipLimit=2.0,
            tileGridSize=(8, 8),
        ).apply(lightness)

        enhanced = cv2.cvtColor(
            cv2.merge((lightness, a, b)),
            cv2.COLOR_LAB2BGR,
        )

        scale = min(
            2.0,
            max(1.0, 420.0 / max(enhanced.shape[:2])),
        )

        if scale > 1.01:
            enhanced = cv2.resize(
                enhanced,
                (
                    max(1, int(round(w * scale))),
                    max(1, int(round(roi.shape[0] * scale))),
                ),
                interpolation=cv2.INTER_CUBIC,
            )

        return enhanced, 0.0, float(y0), float(scale)

    def read(self, img_bgr):
        if img_bgr is None or not getattr(img_bgr, "size", 0):
            return []

        primary, det_count, text_count = self._predict_variant(
            img_bgr,
            variant="original",
        )

        def strong_valid(read):
            _, valid = fix_indian(read.text)

            return (
                valid
                and read.conf >= self.min_ocr_conf
                and read.det_conf >= self.min_detector_conf
            )

        # Retry if original OCR has no valid, sufficiently confident read.
        if not any(strong_valid(read) for read in primary):
            enhanced, ox, oy, scale = self._enhanced_lower_crop(img_bgr)

            fallback, fallback_dets, fallback_texts = (
                self._predict_variant(
                    enhanced,
                    offset_x=ox,
                    offset_y=oy,
                    resize_scale=scale,
                    variant="enhanced_lower",
                )
            )

            log.info(
                "ALPR_RETRY original_detections=%d "
                "original_text_reads=%d retry_detections=%d "
                "retry_text_reads=%d",
                det_count,
                text_count,
                fallback_dets,
                fallback_texts,
            )

            primary.extend(fallback)

        # Remove duplicate detections while retaining distinct candidates.
        best_by_key = {}

        for read in primary:
            fixed, valid = fix_indian(read.text)

            key = (
                fixed if valid else normalize(read.text),
                tuple(round(v / 8) for v in read.bbox),
            )

            score = read.conf * read.det_conf
            previous = best_by_key.get(key)

            if (
                previous is None
                or score > previous.conf * previous.det_conf
            ):
                best_by_key[key] = read

        return sorted(
            best_by_key.values(),
            key=lambda item: item.conf * item.det_conf,
            reverse=True,
        )


class PlateWorker:
    """Runs plate reading asynchronously without blocking tracking."""

    def __init__(self, reader, deterministic=False):
        self.reader = reader
        self.deterministic = bool(deterministic)

        self.jobs = queue.Queue(
            maxsize=0 if self.deterministic else 2
        )

        self.results = queue.Queue()
        self._stop = threading.Event()
        self._accepting = True
        self._state_lock = threading.Lock()
        self.last_ms = 0.0

        self.thread = threading.Thread(
            target=self._run,
            daemon=False,
            name="plates",
        )

        self.thread.start()

    def submit(self, key, crop):
        with self._state_lock:
            if not self._accepting:
                return False

            try:
                self.jobs.put_nowait(
                    (key, crop, time.perf_counter())
                )
                return True
            except queue.Full:
                return False

    def _run(self):
        while True:
            try:
                item = self.jobs.get(timeout=0.1)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue

            try:
                key, crop, queued_at = item
                t0 = time.perf_counter()

                log.info(
                    "OCR_QUEUE key=%s wait_ms=%.1f",
                    key,
                    (t0 - queued_at) * 1000,
                )

                try:
                    if crop is None or getattr(crop, "ndim", 0) != 3 or not crop.size or crop.shape[2] != 3:
                        Diagnostic(
                            "PLATE_CROP_INVALID", "ANPR_PLATE_DETECTION", "plate worker", "validate crop",
                            "validation", "The submitted vehicle crop cannot be read.", True,
                            "missing", "Skipped OCR and returned no plate reads.",
                            severity="WARNING", context={"vehicle_key": key},
                        ).log(log)
                        self.results.put((key, [], None))
                        continue
                    reads = self.reader.read(crop)

                    # Do not log raw plate text or normalized plate values.
                    metadata = [
                        {
                            "length": len(normalize(r.text)),
                            "format_valid": fix_indian(r.text)[1],
                            "ocr_conf": round(r.conf, 3),
                            "detector_conf": round(r.det_conf, 3),
                        }
                        for r in reads
                    ]

                    log.debug(
                        "OCR_RESULT key=%s crop=%sx%s "
                        "reads=%d metadata=%s",
                        key,
                        crop.shape[1],
                        crop.shape[0],
                        len(reads),
                        metadata,
                    )

                except Exception:
                    Diagnostic(
                        "ALPR_INFERENCE_FAILED", "ANPR_OCR", "plate worker", "read crop",
                        "inference", "The combined plate detector and OCR reader failed.",
                        True, "missing", "Returned no plate reads; vehicle processing continues.",
                        context={"vehicle_key": key},
                    ).log(log, exc_info=True)
                    reads = []

                self.last_ms = (
                    time.perf_counter() - t0
                ) * 1000

                log.info(
                    "OCR_TIMING key=%s process_ms=%.1f",
                    key,
                    self.last_ms,
                )

                self.results.put((key, reads, crop))

            finally:
                self.jobs.task_done()

    def wait_idle(self, timeout=60.0):
        """Wait until submitted OCR jobs have finished."""

        end = time.monotonic() + float(timeout)

        while self.jobs.unfinished_tasks:
            if not self.thread.is_alive():
                log.error(
                    "OCR_WAIT_IDLE worker thread is not alive; "
                    "%d job(s) unfinished",
                    self.jobs.unfinished_tasks,
                )
                return False

            if time.monotonic() > end:
                log.error(
                    "OCR_WAIT_IDLE timed out after %.0fs "
                    "with %d job(s) unfinished",
                    timeout,
                    self.jobs.unfinished_tasks,
                )
                return False

            time.sleep(0.0005)

        return True

    def poll(self):
        out = []

        while True:
            try:
                out.append(self.results.get_nowait())
            except queue.Empty:
                return out

    def stop(self, wait=True, timeout=None):
        """Stop accepting new jobs and optionally wait for queued jobs."""

        with self._state_lock:
            self._accepting = False
            self._stop.set()

        if wait:
            self.thread.join(timeout=timeout)
            return not self.thread.is_alive()

        return True
