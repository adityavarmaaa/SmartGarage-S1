import argparse
import json
import logging
import time
from threading import Event
from collections import Counter

import cv2

from .config import load_config, validate_geometry_for_camera
from .common.data_models import usable_frame
from .common.errors import ConfigurationError, Diagnostic, ProcessingError
from .yolo_detector import YoloDetector
from .geometry import Geometry
from .grabber import open_source
from .outbox import Outbox
from .pipeline import Pipeline
from .s2_mapper import S2Mapper
from .uploader import UploadingOutbox
from .plates import PlateReader, PlateWorker

log = logging.getLogger("main")


class MemoryPublisher:
    local_preview = True
    """In-memory publisher for local video testing.

    This deliberately does not contact Service 2 and does not write to
    data/outbox.db. It provides the small publisher interface Pipeline needs.
    """

    def __init__(self):
        self.pending_events = {}
        self.report_events = []

    def publish(self, event):
        """Print the outgoing event payload locally; never contact Service 2."""
        self.report_events.append(json.loads(json.dumps(event, default=str)))
        print(
            "\n========== SERVICE 2 JSON PREVIEW (NOT SENT) ==========",
            flush=True,
        )
        print(
            json.dumps(event, indent=2, ensure_ascii=False, default=str),
            flush=True,
        )
        print("========== END JSON PREVIEW ===========================\n", flush=True)

    def save_pending_event(self, event_key, payload):
        self.pending_events[event_key] = payload

    def load_pending_events(self):
        return list(self.pending_events.items())

    def delete_pending_event(self, event_key):
        self.pending_events.pop(event_key, None)

    def stop(self, wait=True, timeout=5.0):
        return True


def run(
    cfg,
    show=False,
    detector=None,
    publisher=None,
    plate_worker=None,
    source=None,
    no_publisher=False,
    cancel_event=None,
    frame_sink=None,
):
    cancel_event = cancel_event or Event()
    if cancel_event.is_set():
        return None
    # Multi-camera mode uses one shared detector owner and independent
    # per-camera pipelines. The legacy single-camera path below is unchanged.
    if "cameras" in cfg:
        if source is not None:
            raise ValueError("--source override is only supported for single-camera configuration")
        from .multicam import run_multi_camera
        d = cfg["detector"]
        detector = detector or YoloDetector(
            d["model_path"], d["input_size"], d["score_thr"], d["nms_thr"],
            d["classes"], d["threads"],
            device=d.get("device", "cpu"),
        )
        return run_multi_camera(cfg, detector, show=show, no_publisher=no_publisher,
                                cancel_event=cancel_event, frame_sink=frame_sink)

    # Initialize models before starting video capture.
    d = cfg["detector"]
    detector = detector or YoloDetector(
        d["model_path"],
        d["input_size"],
        d["score_thr"],
        d["nms_thr"],
        d["classes"],
        d["threads"],
        device=d.get("device", "cpu"),
    )


    try:
        geometry = Geometry.load(cfg["geometry_file"])
        validate_geometry_for_camera(cfg, geometry)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ProcessingError(Diagnostic(
            "SLOT_GEOMETRY_INVALID", "SLOT_GEOMETRY", "camera geometry",
            "load and validate", "validation", "Camera geometry could not be loaded.",
            False, "failed", "Stopped initialization.",
            context={"camera_id": cfg["camera_id"], "file": cfg["geometry_file"]},
        )) from exc
    if cancel_event.is_set():
        return None
    source = source or open_source(cfg)

    try:
        deadline = time.monotonic() + float(cfg["inference"]["camera_startup_wait_s"])
        first = None
        while (first is None and not cancel_event.is_set()
               and not getattr(source, "eof", False) and time.monotonic() < deadline):
            first = source.read(-1, timeout=min(2.0, max(0, deadline - time.monotonic())))
        if cancel_event.is_set():
            source.stop()
            log.info("VIDEO_CANCELLED before first frame")
            return None
        if first is not None and not usable_frame(first):
            raise ProcessingError(Diagnostic(
                "VIDEO_FRAME_INVALID", "VIDEO_PROCESSING", "capture", "first frame",
                "validation", "Could not calibrate from an invalid starting frame.",
                False, "failed", "Released the source.", context={"camera_id": cfg["camera_id"]},
            ))
        if first is None:
            raise ProcessingError(Diagnostic(
                "VIDEO_FIRST_FRAME_UNAVAILABLE", "VIDEO_PROCESSING", "capture",
                "first frame", "availability", "Source produced no usable frame.",
                False, "failed", "Stopped capture.", context={"camera_id": cfg["camera_id"]},
            ))
    except Exception:
        try:
            source.stop()
        except Exception:
            log.exception("VIDEO_SOURCE_STOP_FAILED during initialization")
        raise

    # Freeze a recorded file on the first frame while the user calibrates the
    # preview window; Space explicitly starts playback. Live streams are not
    # gated here because pausing RTSP decoding may build camera/network buffers.
    if show and getattr(source, "is_file", False) and hasattr(source, "pause"):
        source.pause()

    H, W = first.image.shape[:2]
    log.info("source ready: %dx%d", W, H)

    publisher_was_created = publisher is None

    try:
        if publisher_was_created:
            if no_publisher:
                publisher = MemoryPublisher()
                log.info(
                    "LOCAL TEST MODE: publisher disabled; "
                    "events will not be sent to Service 2"
                )
            else:
                publisher = S2Mapper(
                    UploadingOutbox(
                        cfg["publisher"]["outbox_db"],
                        cfg["publisher"]["s2_url"],
                        timeout=30.0,
                    )
                )


        if plate_worker is None and cfg["plate"]["enabled"]:
            p = cfg["plate"]
            try:
                plate_worker = PlateWorker(PlateReader(
                    p["detector_model"], p["ocr_model"],
                    min_ocr_conf=p.get("min_ocr_conf", 0.5),
                    min_detector_conf=p.get("min_plate_detector_conf", 0.25),
                ))
            except Exception:
                Diagnostic(
                    "ALPR_MODEL_LOAD_FAILED", "ANPR_OCR", "plate worker", "initialize",
                    "initialization", "The plate reader could not initialize.", True,
                    "partial", "Continued vehicle and parking processing without plate reads.",
                    context={"camera_id": cfg["camera_id"]},
                ).log(log, exc_info=True)
        pipe = Pipeline(
            cfg,
            geometry,
            (W, H),
            detector,
            publisher,
            plate_worker,
        )
    except (Exception, SystemExit, KeyboardInterrupt):
        for resource in (source, plate_worker, publisher if publisher_was_created else None):
            if resource is not None:
                try:
                    resource.stop()
                except Exception:
                    log.exception("RUN_RESOURCE_STOP_FAILED during initialization")
        raise

    min_dt = 1.0 / d["max_fps"]
    last_proc_ts, last_idx, last_report = (
        -1e18,
        first.idx - 1,
        time.time(),
    )
    frame = first
    event_counts = Counter()
    paused = False

    try:
        if show:
            cv2.namedWindow("garage-s1", cv2.WINDOW_NORMAL)
            cv2.resizeWindow("garage-s1", 600, 1000)

            # Show the original portrait frame scaled to fit the preview window.
            preview = frame.image
            ph, pw = preview.shape[:2]
            scale = min(600 / pw, 1000 / ph, 1.0)
            if scale < 1.0:
                preview = cv2.resize(
                    preview,
                    (int(pw * scale), int(ph * scale)),
                    interpolation=cv2.INTER_AREA,
                )
            cv2.imshow("garage-s1", preview)
            log.info("Preview ready. Press SPACE to start/pause/resume, or Q to quit.")

            while not cancel_event.is_set():
                key = cv2.waitKey(20) & 0xFF
                if key == ord(" "):
                    if hasattr(source, "resume"):
                        source.resume()
                    break
                if key == ord("q"):
                    cancel_event.set()
                    break

        while frame is not None and not cancel_event.is_set():
            if not usable_frame(frame, first.image.shape):
                Diagnostic(
                    "VIDEO_FRAME_INVALID", "VIDEO_PROCESSING", "capture", "validate",
                    "validation", "Frame is invalid or differs from the calibrated frame size.",
                    True, "missing", "Skipped this frame.",
                    context={"camera_id": cfg["camera_id"], "frame_id": getattr(frame, "idx", None)},
                ).log(log)
                idx = getattr(frame, "idx", None)
                if isinstance(idx, int):
                    last_idx = max(last_idx, idx)
                frame = source.read(last_idx, timeout=1.0)
                continue
            if show and paused:
                # Hold the currently rendered frame and do not ask the source
                # for another one. For local video this also freezes decoding.
                key = cv2.waitKey(30) & 0xFF
                if key == ord(" "):
                    paused = False
                    if hasattr(source, "resume"):
                        source.resume()
                    log.info("Playback resumed")
                elif key == ord("q"):
                    cancel_event.set()
                    break
                continue

            last_idx = frame.idx

            if frame.ts - last_proc_ts >= min_dt * 0.9:
                last_proc_ts = frame.ts

                for ev in pipe.process(frame):
                    event_type = ev["event_type"]
                    event_counts[event_type] += 1

                    log.info(
                        "%s %s plate_status=%s bay=%s t=%s",
                        event_type,
                        ev.get("direction") or "",
                        ev.get("plate_status"),
                        (ev.get("bay") or {}).get("id"),
                        ev.get("event_time"),
                    )

                if show:
                    display_frame = pipe.draw(frame.image.copy())

                    # Fit portrait/landscape preview to the window without
                    # changing the original frame used for detection.
                    screen_w, screen_h = 600, 1000
                    frame_h, frame_w = display_frame.shape[:2]
                    scale = min(
                        screen_w / frame_w,
                        screen_h / frame_h,
                        1.0,
                    )

                    if scale < 1.0:
                        display_frame = cv2.resize(
                            display_frame,
                            (int(frame_w * scale), int(frame_h * scale)),
                            interpolation=cv2.INTER_AREA,
                        )

                    cv2.imshow("garage-s1", display_frame)

                    key = cv2.waitKey(1) & 0xFF
                    if key == ord(" "):
                        paused = True
                        if hasattr(source, "pause"):
                            source.pause()
                        log.info("Playback paused; press SPACE to resume")
                    elif key == ord("q"):
                        cancel_event.set()
                        break

            if paused:
                continue

            if time.time() - last_report > 5:
                log.info(pipe.stats.summary())
                last_report = time.time()

            if cancel_event.is_set():
                break
            frame = source.read(last_idx, timeout=1.0)

            if frame is None and not cancel_event.is_set() and not getattr(source, "eof", False):
                log.warning("no new frame for 1s (camera stalled?)")
                frame = source.read(last_idx, timeout=5.0)
                if frame is None and not cancel_event.is_set() and not getattr(source, "eof", False):
                    raise ProcessingError(Diagnostic(
                        "CAMERA_STREAM_STALLED", "VIDEO_PROCESSING", "capture", "read",
                        "availability", "Capture stalled without reaching end of stream.",
                        False, "partial", "Stopped processing and released capture.",
                        context={"camera_id": cfg["camera_id"], "frame_id": last_idx},
                    ))

        if not cancel_event.is_set() and not pipe.stats.frames:
            raise ProcessingError(Diagnostic(
                "NO_FRAMES_PROCESSED", "VIDEO_PROCESSING", "pipeline", "process",
                "processing", "No frame completed pipeline processing.", False,
                "failed", "Released capture.", context={"camera_id": cfg["camera_id"]},
            ))

    except KeyboardInterrupt:
        cancel_event.set()

    finally:
        if cancel_event.is_set():
            log.info("VIDEO_CANCELLED finalizing processed frames and events")
        try:
            source.stop()
        except Exception:
            log.exception("failed to stop source")

        try:
            if plate_worker is not None:
                stopped = plate_worker.stop(wait=True)

                if not stopped:
                    log.error(
                        "plate worker did not stop; "
                        "OCR results may be incomplete"
                    )

        except Exception:
            log.exception("failed to stop plate worker")

        try:
            flushed_events = pipe.flush(getattr(pipe, "display_ts", first.ts) if cancel_event.is_set() else time.time())
            for ev in flushed_events:
                event_type = ev["event_type"]
                event_counts[event_type] += 1
                log.info(
                    "%s %s plate_status=%s bay=%s t=%s",
                    event_type,
                    ev.get("direction") or "",
                    ev.get("plate_status"),
                    (ev.get("bay") or {}).get("id"),
                    ev.get("event_time"),
                )

            log.info(
                "EVENT SUMMARY: ENTRY=%d PARK_START=%d PARK_END=%d EXIT=%d TOTAL=%d",
                event_counts["ENTRY"],
                event_counts["PARK_START"],
                event_counts["PARK_END"],
                event_counts["EXIT"],
                sum(event_counts.values()),
            )

        except Exception:
            log.exception("pipeline flush failed")

        if publisher_was_created:
            try:
                stopped = publisher.stop(wait=True)

                if not stopped:
                    log.error(
                        "publisher did not stop; "
                        "delivery may continue in the background"
                    )

            except Exception:
                log.exception("failed to stop publisher")

        log.info(pipe.stats.summary())

        # FINAL PARKING JSON REPORT: local preview only; nothing is sent to Service 2.
        if isinstance(publisher, MemoryPublisher):
            try:
                slot_rows = []
                vehicles = {}

                for sid, slot in pipe.slots.slots.items():
                    occ = pipe.slots.occupants.get(sid)
                    vehicle = occ.vehicle if occ is not None else None
                    slot_rows.append({
                        "slot_id": sid,
                        "level": slot.get("level"),
                        "state": ("OCCUPIED" if occ is not None else
                                  "FREE" if pipe.stats.frames else "UNKNOWN"),
                        "track_id": getattr(occ, "track_id", None),
                        "vehicle_uid": getattr(vehicle, "uid", None),
                        "parked_at_epoch": getattr(occ, "parked_at", None),
                        "occupancy_source": getattr(occ, "occupancy_source", None),
                    })

                    if vehicle is not None:
                        from .common import iso
                        color_total = sum(vehicle.color_votes.values())
                        color_count = (
                            vehicle.color_votes.most_common(1)[0][1]
                            if color_total else 0
                        )
                        vehicles[str(vehicle.uid)] = {
                            "vehicle_uid": vehicle.uid,
                            "track_id": getattr(occ, "track_id", None),
                            "first_seen_at": iso(vehicle.first_ts),
                            "parked_at": iso(occ.parked_at) if occ.parked_at is not None else None,
                            "parked_at_epoch": getattr(occ, "parked_at", None),
                            "plate": None,
                            "type": vehicle.vehicle_type(),
                            "color": vehicle.color() or "unknown",
                            "color_confidence": round(color_count / color_total, 3) if color_total else 0.0,
                            "slot_id": sid,
                        }

                events = publisher.report_events
                for ev in events:
                    uid = ev.get("vehicle_uid")
                    if uid is None:
                        continue
                    row = vehicles.setdefault(str(uid), {
                        "vehicle_uid": uid,
                        "track_id": ev.get("track_id"),
                        "plate": None,
                        "type": None,
                        "color": None,
                        "slot_id": None,
                    })
                    detail = ev.get("vehicle") or {}
                    row["plate"] = detail.get("plate") or row["plate"]
                    row["type"] = detail.get("type") or row["type"]
                    row["color"] = detail.get("color") or row["color"]
                    parking = ev.get("parking") or {}
                    bay = ev.get("bay") or {}
                    row["slot_id"] = (
                        parking.get("slot_id") or bay.get("id") or row["slot_id"]
                    )

                occupied = sum(x["state"] == "OCCUPIED" for x in slot_rows)
                report_data = {
                    "report_version": 1,
                    "processing_status": "cancelled" if cancel_event.is_set() else "completed",
                    "camera_id": cfg.get("camera_id"),
                    "parking_area_id": cfg.get("parking_area_id"),
                    "timestamp_note": (
                        "For replayed video, timestamps follow the configured replay "
                        "timeline. INITIAL_SCAN is an observation time, not proof of "
                        "the vehicle's original parking time."
                    ),
                    "summary": {
                        "total_slots": len(slot_rows),
                        "occupied_slots": occupied,
                        "available_slots": sum(row["state"] == "FREE" for row in slot_rows),
                        "event_counts": dict(event_counts),
                        "total_events": len(events),
                    },
                    "slots": slot_rows,
                    "vehicles": list(vehicles.values()),
                    "events": events,
                }
                if cancel_event.is_set() and (cfg.get("report") or {}).get("path"):
                    from .report import write_report
                    write_report(report_data, cfg["report"]["path"])
                print("\n========== FINAL PARKING JSON REPORT ==========", flush=True)
                print(json.dumps(report_data, indent=2, ensure_ascii=False, default=str), flush=True)
                print("========== END FINAL PARKING JSON REPORT ======\n", flush=True)
            except Exception:
                Diagnostic(
                    "REPORT_GENERATION_FAILED", "REPORTING", "single-camera report", "build",
                    "output", "Could not build the final parking report.", True,
                    "partial", "Kept previously processed events.",
                    context={"camera_id": cfg["camera_id"]},
                ).log(log, exc_info=True)



def cli():
    ap = argparse.ArgumentParser(
        description="Garage vision service (S1)"
    )

    ap.add_argument(
        "--config",
        default="config/config.local.yaml",
    )

    ap.add_argument(
        "--source",
        help="video file or rtsp:// URL (overrides config)",
    )

    ap.add_argument(
        "--replay",
        choices=["realtime", "sync"],
        help="file replay mode",
    )

    ap.add_argument(
        "--show",
        action="store_true",
        help="show debug window (SPACE starts/pauses/resumes; Q quits)",
    )

    ap.add_argument(
        "--no-plates",
        action="store_true",
        help="disable plate reading (speed test)",
    )

    ap.add_argument(
        "--no-publisher",
        action="store_true",
        help="disable Service 2 publishing for local video testing",
    )

    a = ap.parse_args()

    over = {}

    if a.source:
        over["source"] = a.source

    if a.replay:
        over["replay"] = a.replay

    if a.no_plates:
        over["plate"] = {"enabled": False}

    try:
        from .obs import setup_logging
        setup_logging(level="DEBUG", log_dir="logs")
    except Exception as exc:  # never let logging setup block a run
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        logging.getLogger("main").error("production logging setup failed (%s); using basic logging", exc)

    try:
        cfg = load_config(a.config, over)
        if a.source and "cameras" in cfg:
            ap.error("--source requires a single-camera config")
        if a.no_plates:
            for camera in cfg.get("cameras", []):
                camera["plate"] = {**camera.get("plate", {}), "enabled": False}
        run(cfg, show=a.show, no_publisher=a.no_publisher)
    except (ConfigurationError, ProcessingError) as exc:
        exc.diagnostic.log(log, exc_info=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    cli()

