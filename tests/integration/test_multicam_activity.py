"""Focused tests for multi-camera activity scoring (grayscale + Lucas-Kanade)."""
import cv2
import numpy as np

from s1.multicam import _activity_score, _lucas_kanade_score


def _textured_frame():
    rng = np.random.default_rng(7)
    gray = np.zeros((90, 160), dtype=np.uint8)
    # Stable, high-contrast texture gives Lucas-Kanade trackable features.
    for y in range(8, 90, 12):
        for x in range(8, 160, 12):
            cv2.rectangle(gray, (x - 2, y - 2), (x + 2, y + 2), 180, -1)
    noise = rng.integers(0, 8, gray.shape, dtype=np.uint8)
    gray = cv2.add(gray, noise)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def test_static_scene_has_no_motion_score():
    frame = _textured_frame()
    previous = cv2.cvtColor(
        cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA),
        cv2.COLOR_BGR2GRAY,
    )
    current_gray, score = _activity_score(previous, frame)
    assert current_gray.shape == (90, 160)
    assert score == 0.0


def test_lucas_kanade_detects_translated_features():
    frame = _textured_frame()
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    matrix = np.float32([[1, 0, 3], [0, 1, 0]])
    moved = cv2.warpAffine(gray, matrix, (160, 90))
    score = _lucas_kanade_score(gray, moved)
    assert score > 0.0


def test_activity_score_preserves_existing_grayscale_signal():
    frame = _textured_frame()
    previous = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    changed = frame.copy()
    cv2.rectangle(changed, (60, 25), (110, 70), (255, 255, 255), -1)
    gray, score = _activity_score(previous, changed)
    assert gray.shape == (90, 160)
    assert 0.0 < score <= 1.0
