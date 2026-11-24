"""Health check. Implementation: vehicle_intelligence.api.health.

Importing this module does not load the API. The function is resolved on use.
"""


def __getattr__(name):
    if name == "health":
        from vehicle_intelligence.api import health
        return health
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
