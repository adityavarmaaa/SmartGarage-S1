"""Encrypted best-plate evidence with original and CLAHE PNG variants."""
import logging
import os
import tempfile

import cv2

from s1.security import encrypt_bytes, encrypt_text, plate_hmac

log = logging.getLogger("plate_evidence")


def clahe_enhance(crop_bgr, clip=2.0, grid=8, min_width=240):
    """CLAHE on luminance only; preserve colour and upscale tiny crops."""
    lab = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2LAB)
    lightness, a, b = cv2.split(lab)
    lightness = cv2.createCLAHE(
        clipLimit=clip, tileGridSize=(grid, grid)
    ).apply(lightness)
    out = cv2.cvtColor(cv2.merge((lightness, a, b)), cv2.COLOR_LAB2BGR)

    if out.shape[1] < min_width:
        scale = min_width / float(out.shape[1])
        out = cv2.resize(
            out,
            (min_width, max(1, int(round(out.shape[0] * scale)))),
            interpolation=cv2.INTER_CUBIC,
        )
    return out


def _sharpness(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _safe(text):
    return "".join(
        ch if ch.isalnum() or ch in "-_" else "_"
        for ch in str(text)
    )


def _vehicles(rt):
    seen = {}
    pipe = rt.pipeline

    for vehicle in list(pipe.registry.by_uid.values()):
        seen[vehicle.uid] = vehicle

    for pending in list(
        getattr(pipe, "pending_parking_events", []) or []
    ):
        vehicle = pending.get("vehicle")
        if vehicle is not None:
            seen.setdefault(vehicle.uid, vehicle)

    return [seen[key] for key in sorted(seen)]


def _write_encrypted(path, image):
    """Encode PNG in memory, encrypt it, then atomically write ciphertext."""
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError("PNG encoding failed")

    encrypted = encrypt_bytes(encoded.tobytes())
    if not encrypted:
        raise RuntimeError("Image encryption returned no data")

    directory = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(
        prefix=".plate-evidence-",
        suffix=".tmp",
        dir=directory,
    )

    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encrypted)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def save_best_plate_evidence(runtimes, out_dir):
    """Save encrypted valid and diagnostic plate-image evidence."""
    records = []

    for cam in sorted(runtimes):
        rt = runtimes[cam]
        if rt.pipeline is None:
            continue

        try:
            vehicles = _vehicles(rt)
        except Exception:
            log.exception(
                "PLATE_EVIDENCE cannot enumerate vehicles camera=%s", cam
            )
            continue

        for vehicle in vehicles:
            store = getattr(vehicle, "plate_crops", None) or {}
            diagnostic_crop = getattr(
                vehicle, "plate_diagnostic_crop", None
            )

            if not store and (
                diagnostic_crop is None
                or not getattr(diagnostic_crop, "size", 0)
            ):
                continue

            try:
                folder = os.path.join(out_dir, _safe(cam))
                os.makedirs(folder, exist_ok=True)

                stem = (
                    f"{_safe(vehicle.slot_id or 'NOSLOT')}_"
                    f"{_safe(vehicle.uid[:8])}"
                )

                # Existing valid-format evidence path remains unchanged.
                if store:
                    consensus = vehicle.voter.consensus()
                    plate_text = (
                        consensus.text
                        if consensus is not None and consensus.text in store
                        else None
                    )

                    if plate_text is None:
                        plate_text = max(
                            sorted(store),
                            key=lambda key: store[key][0],
                        )

                    confidence, crop = store[plate_text]

                    if crop is not None and getattr(crop, "size", 0):
                        original_path = os.path.join(
                            folder, stem + "_original.png.enc"
                        )
                        clahe_path = os.path.join(
                            folder, stem + "_clahe.png.enc"
                        )

                        enhanced = clahe_enhance(crop)

                        _write_encrypted(original_path, crop)
                        _write_encrypted(clahe_path, enhanced)

                        records.append({
                            "camera_id": cam,
                            "vehicle_uid": vehicle.uid,
                            "slot_id": vehicle.slot_id,
                            "plate_text": None,
                            "plate_text_ciphertext": encrypt_text(str(plate_text)),
                            "plate_text_hmac": plate_hmac(str(plate_text)),
                            "ocr_confidence": round(float(confidence), 4),
                            "plate_status": vehicle.plate_status,
                            "crop_px": [
                                int(crop.shape[1]),
                                int(crop.shape[0]),
                            ],
                            "sharpness_original": round(_sharpness(crop), 2),
                            "sharpness_clahe": round(_sharpness(enhanced), 2),
                            "original_png": original_path,
                            "clahe_png": clahe_path,
                            "image_encryption": "AES-256-GCM",
                        })

                # Invalid-format crop is encrypted separately.
                # No OCR text, ciphertext, or HMAC is stored in this record.
                if (
                    diagnostic_crop is not None
                    and getattr(diagnostic_crop, "size", 0)
                ):
                    diagnostic_original = os.path.join(
                        folder, stem + "_diagnostic_original.png.enc"
                    )
                    diagnostic_clahe = os.path.join(
                        folder, stem + "_diagnostic_clahe.png.enc"
                    )

                    diagnostic_enhanced = clahe_enhance(diagnostic_crop)

                    _write_encrypted(diagnostic_original, diagnostic_crop)
                    _write_encrypted(diagnostic_clahe, diagnostic_enhanced)

                    records.append({
                        "camera_id": cam,
                        "vehicle_uid": vehicle.uid,
                        "slot_id": vehicle.slot_id,
                        "plate_text": None,
                        "plate_text_ciphertext": None,
                        "plate_text_hmac": None,
                        "ocr_confidence": round(
                            float(getattr(
                                vehicle, "plate_diagnostic_confidence", 0.0
                            )),
                            4,
                        ),
                        "plate_status": vehicle.plate_status,
                        "evidence_type": "diagnostic_unvalidated",
                        "crop_px": [
                            int(diagnostic_crop.shape[1]),
                            int(diagnostic_crop.shape[0]),
                        ],
                        "sharpness_original": round(
                            _sharpness(diagnostic_crop), 2
                        ),
                        "sharpness_clahe": round(
                            _sharpness(diagnostic_enhanced), 2
                        ),
                        "original_png": diagnostic_original,
                        "clahe_png": diagnostic_clahe,
                        "image_encryption": "AES-256-GCM",
                    })

            except Exception:
                log.exception(
                    "PLATE_EVIDENCE failed camera=%s uid=%s",
                    cam,
                    getattr(vehicle, "uid", "?"),
                )

    log.info(
        "PLATE_EVIDENCE encrypted=%d dir=%s",
        len(records),
        out_dir,
    )
    return records
