"""Worker-only cache identities; destinations must be supplied explicitly."""
from dataclasses import asdict, is_dataclass


def database_cache_scope(config=None):
    if config is None:
        raise ValueError("Worker database configuration must be explicit")
    from ..context import database_scope
    return database_scope(asdict(config) if is_dataclass(config) else config)
