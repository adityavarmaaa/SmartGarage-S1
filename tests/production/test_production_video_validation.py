import cv2
import numpy as np

from scripts.production_video_validation import annotate_frame, pad_for_video_writer


def test_annotation_preserves_pixel_detail_and_input():
    rows, cols = np.indices((128, 192))
    image = np.stack([(cols % 2) * 255, (rows % 2) * 255, (cols % 3) * 100], axis=-1).astype(np.uint8)
    original = image.copy()
    annotated = annotate_frame(image, [], 100, 0)
    assert annotated.shape == original.shape and annotated.dtype == np.uint8
    assert np.array_equal(image, original)
    assert np.array_equal(annotated[65:], original[65:])
    assert not np.array_equal(annotated[:65], original[:65])


def test_encoder_padding_keeps_original_pixels_and_decodes(tmp_path):
    frame = np.full((95, 143, 3), (20, 100, 220), dtype=np.uint8)
    padded = pad_for_video_writer(annotate_frame(frame, [], 100, 0))
    assert padded.shape == (96, 144, 3)
    assert np.array_equal(padded[:95, :143], annotate_frame(frame, [], 100, 0))
    target = str(tmp_path / "probe.mp4")
    writer = cv2.VideoWriter(target, cv2.VideoWriter_fourcc(*"mp4v"), 6, (144, 96))
    assert writer.isOpened()
    try:
        writer.write(padded)
    finally:
        writer.release()
    capture = cv2.VideoCapture(target)
    try:
        ok, decoded = capture.read()
        assert ok and decoded.shape == padded.shape
        assert np.abs(decoded[70, 70].astype(int) - frame[70, 70]).max() <= 12
    finally:
        capture.release()
