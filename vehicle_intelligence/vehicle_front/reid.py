import logging
import cv2
import numpy as np

log = logging.getLogger("reid")


class OSNetEmbedder:
    """OSNet vehicle ReID. device: 'cpu' or 'cuda[:N]' (needs onnxruntime-gpu)."""

    def __init__(self, model_path, device="cpu"):
        import onnxruntime as ort
        providers = ["CPUExecutionProvider"]
        if str(device).startswith("cuda"):
            idx = int(str(device).split(":")[1]) if ":" in str(device) else 0
            providers = [("CUDAExecutionProvider", {"device_id": idx}),
                         "CPUExecutionProvider"]
        self.session = ort.InferenceSession(str(model_path), providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        active = self.session.get_providers()
        self.on_gpu = "CUDAExecutionProvider" in active
        if str(device).startswith("cuda") and not self.on_gpu:
            log.warning("OSNet requested %s but CUDAExecutionProvider is not "
                        "available (pip install onnxruntime-gpu); running on CPU", device)
        print("OSNet ready:", active)

    def encode(self, bgr_crop):
        if bgr_crop is None or bgr_crop.size == 0:
            return None
        # Converted OpenVINO ONNX expects BGR, 208x208.
        image = cv2.resize(bgr_crop, (208, 208))
        tensor = np.transpose(image.astype(np.float32), (2, 0, 1))[None]
        vector = self.session.run([self.output_name], {self.input_name: tensor})[0][0].astype(np.float32)
        norm = float(np.linalg.norm(vector))
        if not np.isfinite(vector).all() or norm <= 1e-12:
            return None
        return vector / norm


class HistogramEmbedder:
    """Colour-layout fallback used ONLY when the OSNet ONNX file is missing.

    512-d L2-normalised vector: 2x2 spatial grid x (16 hue x 4 sat x 2 val) HSV
    histogram. It separates cars of different colour, but cannot tell two cars of
    the same colour apart, so global_reid uses a stricter threshold with it and
    the final report labels the backend as "histogram_fallback".
    """
    on_gpu = False
    name = "histogram_fallback"

    def encode(self, bgr_crop):
        if bgr_crop is None or bgr_crop.size == 0:
            return None
        img = cv2.resize(bgr_crop, (64, 64), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        parts = []
        for gy in range(2):
            for gx in range(2):
                cell = hsv[gy * 32:(gy + 1) * 32, gx * 32:(gx + 1) * 32]
                hist = cv2.calcHist([cell], [0, 1, 2], None, [16, 4, 2],
                                    [0, 180, 0, 256, 0, 256]).reshape(-1)
                parts.append(hist.astype(np.float32))
        vec = np.concatenate(parts)
        norm = float(np.linalg.norm(vec))
        if not np.isfinite(norm) or norm <= 1e-12:
            return None
        return (vec / norm).astype(np.float32)
