from __future__ import annotations

import contextlib
import json
import logging
import threading
from contextvars import ContextVar

_capture = ContextVar("cm_log_capture", default=None)

MARKER = b"\n... output truncated ...\n"


class BoundedBuffer:
    """UTF-8 head/tail capture with bounded retained memory, including marker."""

    def __init__(self, max_bytes=256_000, redactor=None):
        self.limit = max(0, int(max_bytes))
        self.redactor = redactor
        self.head = b""
        self.tail = b""
        self.truncated = False
        self.total = 0

    def write(self, value):
        length = len(value)
        try:
            value = self.redactor(value) if self.redactor else value
            data = value.encode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — capture and representation must never fail a task
            data = b"[log redaction failed; output withheld]\n"
        self.total += len(data)
        self.truncated = self.total > self.limit
        head_size = max(0, (self.limit - min(len(MARKER), self.limit)) // 2)
        tail_size = self.limit - head_size
        take = min(max(0, head_size - len(self.head)), len(data))
        self.head += data[:take]
        self.tail = (self.tail + data[take:])[-tail_size:] if tail_size else b""
        return length

    def flush(self):
        pass

    def getvalue(self):
        if not self.truncated:
            data = self.head + self.tail
        else:
            marker = MARKER[:self.limit]
            remaining = self.limit - len(marker) - len(self.head)
            data = self.head + marker + (self.tail[-remaining:] if remaining else b"")
        return data.decode("utf-8", errors="ignore")


def result_fields(value, config=None):
    limit = config.result_max_bytes if config else 64_000
    try:
        text = json.dumps(value, ensure_ascii=False, default=repr)
    except Exception:  # noqa: BLE001 — capture and representation must never fail a task
        try:
            text = repr(value)
        except Exception:  # noqa: BLE001 — capture and representation must never fail a task
            text = "<unrepresentable result>"
    buffer = BoundedBuffer(limit)
    buffer.write(text)
    return {"result_repr": buffer.getvalue(), "result_truncated": buffer.truncated}


@contextlib.contextmanager
def capture_logs(config=None):
    buffer = BoundedBuffer(
        config.log_capture_max_bytes if config else 256_000,
        config.log_redactor if config else None,
    )
    if config is not None and not config.log_capture:
        yield buffer
        return
    owner = threading.get_ident()
    token = _capture.set(buffer)
    handler = logging.StreamHandler(buffer)
    handler.setLevel(config.log_capture_level if config else 0)
    prefixes = config.log_capture_loggers if config else ()
    handler.addFilter(lambda record: record.thread == owner and _capture.get() is buffer and (
        not prefixes or any(record.name == p or record.name.startswith(p + ".") for p in prefixes)
    ))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield buffer
    finally:
        root.removeHandler(handler)
        _capture.reset(token)
