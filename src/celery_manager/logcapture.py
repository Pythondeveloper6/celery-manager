from __future__ import annotations

import contextlib
import io
import logging


@contextlib.contextmanager
def capture_logs():
    """Capture root-logger output (and anything a task logs) into a buffer."""
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield buffer
    finally:
        root.removeHandler(handler)
