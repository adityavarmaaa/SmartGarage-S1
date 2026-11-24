"""Error ledger. Implementation: vehicle_intelligence.common.logging_config."""
from vehicle_intelligence._compat import export_module as _export_module

_export_module("vehicle_intelligence.common.logging_config", globals())
del _export_module

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import logging


@dataclass(frozen=True)
class Diagnostic:
    code: str
    module: str
    component: str
    operation: str
    category: str
    message: str
    recoverable: bool
    result: str
    action: str
    severity: str = "ERROR"
    context: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def as_dict(self):
        return asdict(self)

    def log(self, logger, *, exc_info=False):
        data = self.as_dict()
        logger.log(
            getattr(logging, self.severity), "%s", json.dumps(data, default=str),
            extra={"diagnostic": data}, exc_info=exc_info,
        )


class ProcessingError(RuntimeError):
    def __init__(self, diagnostic):
        self.diagnostic = diagnostic
        super().__init__(f"{diagnostic.code}: {diagnostic.message}")


class ConfigurationError(ValueError):
    def __init__(self, diagnostic):
        self.diagnostic = diagnostic
        super().__init__(f"{diagnostic.code}: {diagnostic.message}")
