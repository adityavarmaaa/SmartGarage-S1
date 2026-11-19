"""Deterministic replay support.

Why this exists
---------------
When a recorded file is replayed "in real time", the result depends on how fast
the machine happens to be on that particular run: frames are dropped when the
latest-frame buffer overflows, the motion gate and the scheduler use the wall
clock, OCR results arrive whenever the worker thread finishes, and plate-wait
deadlines are measured in wall seconds. Two runs on the SAME video can
therefore disagree.

Deterministic mode removes every one of those dependencies for FILE sources:

* every frame is read in order (no drops);
* the scheduler, motion gate and plate deadlines run on the VIDEO clock;
* OCR / ReID results are applied before the next frame (no thread races);
* the shared batch composition and ordering are fixed;
* RNGs and cuDNN are pinned.

Live cameras (RTSP / webcam) are never affected: ``auto`` only switches the
mode on when EVERY configured source is a file.
"""
import logging
import os
import random
import time
from dataclasses import dataclass

log = logging.getLogger("determinism")

_NETWORK_PREFIXES = ("rtsp://", "rtsps://", "http://", "https://", "rtmp://", "udp://", "tcp://")


def is_file_source(source):
    """True for a path to a recorded file; False for RTSP/HTTP/device index."""
    if isinstance(source, int):
        return False
    text = str(source).strip().lower()
    if not text or text.startswith(_NETWORK_PREFIXES) or text.isdigit():
        return False
    return True


@dataclass(frozen=True)
class DeterminismSettings:
    enabled: bool
    reason: str
    seed: int
    timestamp_source: str      # "laptop" | "video_metadata"
    run_start_epoch: float     # laptop clock captured ONCE for the whole run

    def as_dict(self):
        return {
            "enabled": self.enabled,
            "reason": self.reason,
            "seed": self.seed,
            "timestamp_source": self.timestamp_source,
            "run_start_epoch": self.run_start_epoch,
        }


def resolve_deterministic(cfg, camera_cfgs, now=None):
    """Decide whether this run is deterministic. Never raises."""
    section = cfg.get("determinism", {}) or {}
    seed = int(section.get("seed", 1234))
    ts_source = str(section.get("timestamp_source", "laptop")).strip().lower()
    if ts_source not in ("laptop", "video_metadata"):
        log.warning("determinism.timestamp_source=%r is not valid; using 'laptop'", ts_source)
        ts_source = "laptop"
    fixed = section.get("fixed_start_epoch")   # only for tests / audits that must be bit-identical
    start = float(fixed if fixed is not None else (now if now is not None else time.time()))

    mode = cfg.get("deterministic", "auto")
    if isinstance(mode, str):
        mode = mode.strip().lower()
    if mode in (True, "true", "on", "yes", "1"):
        enabled, reason = True, "forced_on_by_config"
    elif mode in (False, "false", "off", "no", "0"):
        enabled, reason = False, "forced_off_by_config"
    else:
        live = [c["camera_id"] for c in camera_cfgs if not is_file_source(c.get("source"))]
        if live:
            enabled, reason = False, "auto_off_live_sources:" + ",".join(live)
        else:
            enabled, reason = True, "auto_on_all_sources_are_files"
    return DeterminismSettings(enabled, reason, seed, ts_source, start)


def seed_everything(seed):
    """Pin every RNG we can reach. Each step is best-effort and logged."""
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    random.seed(seed)
    pinned = ["python.random"]
    try:
        import numpy as np
        np.random.seed(seed)
        pinned.append("numpy")
    except Exception as exc:
        log.warning("numpy seeding skipped: %s", exc)
    try:
        import cv2
        cv2.setRNGSeed(seed)
        pinned.append("cv2")
    except Exception as exc:
        log.warning("cv2 seeding skipped: %s", exc)
    try:
        import torch
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False   # autotuning picks kernels per run
        try:
            torch.use_deterministic_algorithms(True, warn_only=True)
        except Exception as exc:
            log.warning("torch.use_deterministic_algorithms unavailable: %s", exc)
        pinned.append("torch+cudnn")
    except Exception as exc:
        log.info("torch not available for seeding (%s); YOLO determinism not pinned", exc.__class__.__name__)
    log.info("DETERMINISM seed=%d pinned=%s", seed, ",".join(pinned))
    return pinned
