"""Health check. Implementation: s1.api.health.

Importing this module does not load the API. The function is resolved on use.
"""


def __getattr__(name):
    if name == "health":
        from s1.api import health
        return health
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
