from collections import Counter

import cv2
import numpy as np

TYPE_BY_COCO = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}


CHROMATIC_NAMES = (
    "red", "orange", "yellow", "green", "blue", "purple", "brown"
)


def classify_pixels(hsv):
    """Classify chromatic pixels using OpenCV HSV hue ranges."""
    H = hsv[:, 0].astype(np.float32)
    S = hsv[:, 1].astype(np.float32)
    V = hsv[:, 2].astype(np.float32)

    names = np.full(len(hsv), "other", dtype=object)
    chromatic = S >= 55

    names[chromatic & ((H < 8) | (H >= 170))] = "red"
    names[chromatic & (H >= 8) & (H < 20)] = "orange"
    names[chromatic & (H >= 20) & (H < 35)] = "yellow"
    names[chromatic & (H >= 35) & (H < 85)] = "green"
    names[chromatic & (H >= 85) & (H < 130)] = "blue"
    names[chromatic & (H >= 130) & (H < 155)] = "purple"
    names[chromatic & (H >= 155) & (H < 170)] = "red"

    # Dark orange/brown tones
    names[
        chromatic & (H >= 5) & (H < 25) & (V < 125)
    ] = "brown"

    return names


def dominant_color_details(crop_bgr):
    """
    Estimate vehicle body colour.
    HSV identifies chromatic colours.
    CIELAB separates neutral colours more robustly than brightness alone.

    Returned agreement is a pixel agreement score, NOT a probability.
    """
    if not isinstance(crop_bgr, np.ndarray):
        return None, 0.0

    if crop_bgr.ndim != 3 or crop_bgr.shape[2] != 3:
        return None, 0.0

    h, w = crop_bgr.shape[:2]

    if h < 30 or w < 30:
        return None, 0.0

    # Central body region: avoid much of the background and box boundary.
    y1, y2 = int(h * 0.28), int(h * 0.78)
    x1, x2 = int(w * 0.15), int(w * 0.85)

    roi = crop_bgr[y1:y2, x1:x2]

    if roi.size == 0:
        return None, 0.0

    roi = cv2.resize(roi, (100, 70), interpolation=cv2.INTER_AREA)

    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    lab = cv2.cvtColor(roi, cv2.COLOR_BGR2LAB)

    hp = hsv.reshape(-1, 3)
    lp = lab.reshape(-1, 3)

    H = hp[:, 0].astype(np.float32)
    S = hp[:, 1].astype(np.float32)
    V = hp[:, 2].astype(np.float32)

    L = lp[:, 0].astype(np.float32) * (100.0 / 255.0)
    A = lp[:, 1].astype(np.float32) - 128.0
    B = lp[:, 2].astype(np.float32) - 128.0

    # Detect genuinely dark, low-saturation vehicle bodies before
    # excluding deep shadows from normal colour voting.
    dark_fraction = float(np.mean((V < 55) & (S < 65)))

    if dark_fraction >= 0.80:
        return "black", round(dark_fraction, 3)

    # Reject deep shadows and extreme highlights from normal voting.
    usable = (V >= 35) & (V <= 250)

    if np.count_nonzero(usable) < 150:
        return None, 0.0

    # 1. Chromatic colours: HSV hue + saturation.
    chromatic_mask = usable & (S >= 65)
    chromatic_labels = classify_pixels(hp)

    chromatic_votes = Counter(
        str(name)
        for name in chromatic_labels[chromatic_mask]
        if name in CHROMATIC_NAMES
    )

    chromatic_total = sum(chromatic_votes.values())
    usable_total = int(np.count_nonzero(usable))

    if chromatic_total >= max(100, int(0.16 * usable_total)):
        name, count = chromatic_votes.most_common(1)[0]
        agreement = count / chromatic_total
        support = count / usable_total

        if agreement >= 0.55 and support >= 0.12:
            return name, round(float(agreement), 3)

    # 2. Neutral colours: use LAB lightness and chroma.
    # Low chroma means the pixel is close to a neutral grey axis.
    lab_chroma = np.sqrt(A * A + B * B)

    neutral_mask = usable & (lab_chroma <= 22)

    if np.count_nonzero(neutral_mask) < 100:
        return None, 0.0

    neutral_L = L[neutral_mask]

    neutral_labels = np.full(len(neutral_L), "other", dtype=object)

    neutral_labels[neutral_L < 27] = "black"
    neutral_labels[
        (neutral_L >= 27) & (neutral_L < 53)
    ] = "gray"
    neutral_labels[
        (neutral_L >= 53) & (neutral_L < 78)
    ] = "silver"
    neutral_labels[neutral_L >= 78] = "white"

    votes = Counter(
        str(name)
        for name in neutral_labels
        if name in ("black", "gray", "silver", "white")
    )

    total = sum(votes.values())

    if total < max(100, int(0.12 * usable_total)):
        return None, 0.0

    name, count = votes.most_common(1)[0]
    agreement = count / total
    support = count / usable_total

    if agreement < 0.60 or support < 0.10:
        return None, round(float(agreement), 3)

    return name, round(float(agreement), 3)


def dominant_color(crop_bgr):
    """Compatibility wrapper."""
    colour, _agreement = dominant_color_details(crop_bgr)
    return colour
