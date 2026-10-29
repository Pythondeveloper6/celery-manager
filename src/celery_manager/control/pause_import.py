"""Validated, atomic import of legacy schedule pauses. Dry-run by default."""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone

from celery_manager.db.models import AuditLog, PeriodicSchedule

MAX_ROWS = 5000
MAX_BYTES = 1_000_000


def import_pauses(session, text: str, actor: str, *, dry_run: bool = True, now=None):
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("CSV must be at most 1 MB.")
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames or set(reader.fieldnames) != {"schedule_name", "paused_until"}:
        raise ValueError("CSV columns must be schedule_name,paused_until; blank paused_until means indefinite.")
    now = now or datetime.now(timezone.utc)
    planned, names = [], set()
    for number, item in enumerate(reader, start=2):
        if number > MAX_ROWS + 1:
            raise ValueError(f"CSV must contain at most {MAX_ROWS} rows.")
        name = (item.get("schedule_name") or "").strip()
        if not name or name in names or None in item:
            raise ValueError(f"Row {number}: missing/duplicate schedule or extra columns.")
        names.add(name)
        value = (item.get("paused_until") or "").strip()
        try:
            until = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
        except ValueError as exc:
            raise ValueError(f"Row {number}: paused_until must be an ISO timestamp with an offset.") from exc
        if until is not None:
            if until.tzinfo is None or until <= now:
                raise ValueError(f"Row {number}: paused_until must be a future timestamp with an offset.")
            until = until.astimezone(timezone.utc)
        row = session.query(PeriodicSchedule).filter_by(name=name).one_or_none()
        if row is None:
            raise ValueError(f"Row {number}: unknown schedule {name!r}.")
        planned.append((row, until))
    result = {"dry_run": dry_run, "count": len(planned), "items": [
        {"schedule_name": row.name, "paused_until": until.isoformat() if until else None}
        for row, until in planned
    ]}
    if not dry_run:
        # All validation precedes all writes, and the audit shares the commit.
        for row, until in planned:
            row.paused_at, row.paused_by, row.paused_until = now, str(actor), until
        session.add(AuditLog(username=str(actor), actor_subject=getattr(actor, "subject", None),
                             action="pause_import", target_type="schedule", target_id="*",
                             details=json.dumps(result), created_at=now))
        session.commit()
    return result
