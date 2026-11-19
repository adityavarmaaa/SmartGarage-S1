"""Production logging: run id, rotating file, per-component error ledger."""
import logging
import logging.handlers
import os
import threading
import time
import uuid
from collections import Counter, deque

_LEDGER = None
_LOCK = threading.Lock()


class ErrorLedger(logging.Handler):
    """Counts WARNING / ERROR records per logger so the final report can say
    honestly how healthy the run was (instead of errors scrolling away)."""

    def __init__(self, keep=50):
        super().__init__(level=logging.WARNING)
        self.warnings = Counter()
        self.errors = Counter()
        self.recent = deque(maxlen=keep)
        self._guard = threading.Lock()

    def emit(self, record):
        try:
            with self._guard:
                bucket = self.errors if record.levelno >= logging.ERROR else self.warnings
                bucket[record.name] += 1
                if record.levelno >= logging.ERROR:
                    self.recent.append({
                        "logger": record.name,
                        "diagnostic": getattr(record, "diagnostic", None),
                        "level": record.levelname,
                        "message": record.getMessage()[:300],
                        "exception": record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else None,
                    })
        except Exception:
            self.handleError(record)

    def snapshot(self):
        with self._guard:
            return {
                "error_count": int(sum(self.errors.values())),
                "warning_count": int(sum(self.warnings.values())),
                "errors_by_component": dict(sorted(self.errors.items())),
                "warnings_by_component": dict(sorted(self.warnings.items())),
                "recent_errors": list(self.recent),
            }


class _RunIdFilter(logging.Filter):
    def __init__(self, run_id):
        super().__init__()
        self.run_id = run_id

    def filter(self, record):
        record.run_id = self.run_id
        return True


def get_ledger():
    return _LEDGER


def setup_logging(level="INFO", log_dir="logs", run_id=None, quiet=False):
    """Idempotent. Console + rotating file (10 MB x 5) + error ledger."""
    global _LEDGER
    with _LOCK:
        run_id = run_id or time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        root = logging.getLogger()
        for h in list(root.handlers):
            if getattr(h, "_s1_managed", False):
                root.removeHandler(h)
                h.close()
        root.setLevel(logging.DEBUG if str(level).upper() == "DEBUG" else logging.INFO)
        fmt = logging.Formatter(
            "%(asctime)s %(levelname)s [%(run_id)s] %(name)s: %(message)s")
        flt = _RunIdFilter(run_id)

        console = logging.StreamHandler()
        console.setLevel(logging.WARNING if quiet else logging.INFO)
        console.setFormatter(fmt)
        console.addFilter(flt)
        console._s1_managed = True
        root.addHandler(console)

        try:
            os.makedirs(log_dir, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                os.path.join(log_dir, "s1.log"), maxBytes=10 * 1024 * 1024,
                backupCount=5, encoding="utf-8")
            fh.setLevel(logging.DEBUG if str(level).upper() == "DEBUG" else logging.INFO)
            fh.setFormatter(fmt)
            fh.addFilter(flt)
            fh._s1_managed = True
            root.addHandler(fh)
        except OSError as exc:
            # Read-only disk etc.: keep running on console only.
            root.warning("file logging disabled (%s)", exc)

        _LEDGER = ErrorLedger()
        _LEDGER._s1_managed = True
        root.addHandler(_LEDGER)
        logging.getLogger("s1.obs").info("LOGGING_READY run_id=%s dir=%s", run_id, os.path.abspath(log_dir))
        return run_id
