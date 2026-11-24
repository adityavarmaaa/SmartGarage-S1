import datetime as dt
import math
from numbers import Integral, Real
from dataclasses import dataclass

import numpy as np


@dataclass
class Frame:
    ts: float          # CAPTURE time (epoch seconds), set when the frame left the decoder
    image: np.ndarray  # BGR full-resolution image
    idx: int           # running frame counter from the source


def usable_frame(frame, expected_shape=None):
    """Validate a decoded BGR frame before it can advance pipeline state."""
    image = getattr(frame, "image", None)
    idx = getattr(frame, "idx", None)
    ts = getattr(frame, "ts", None)
    return (
        isinstance(image, np.ndarray) and image.dtype == np.uint8
        and image.ndim == 3 and image.shape[2] == 3 and image.size > 0
        and (expected_shape is None or image.shape == expected_shape)
        and isinstance(idx, Integral) and idx >= 0
        and isinstance(ts, Real) and math.isfinite(ts)
    )


def iso(ts: float) -> str:
    """Convert Unix epoch seconds to an ISO-8601 timestamp in Indian Standard Time."""
    from zoneinfo import ZoneInfo

    value = dt.datetime.fromtimestamp(float(ts), tz=dt.timezone.utc)
    return value.astimezone(ZoneInfo("Asia/Kolkata")).isoformat(
        timespec="milliseconds"
    )

