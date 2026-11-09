"""Minimal timing interface for isolated collectors; never records SQL or inputs."""
from contextlib import contextmanager
from time import perf_counter


@contextmanager
def stage(name, **metadata):
    started = perf_counter()
    measurements = {key: value for key, value in metadata.items() if type(value) in (bool, int, float)}
    try:
        yield measurements
    finally:
        measurements["elapsed_ms"] = round((perf_counter() - started) * 1000, 2)
