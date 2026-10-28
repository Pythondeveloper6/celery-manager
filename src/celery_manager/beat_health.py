"""Optional beat tick persistence; no migration or task dispatch in beat."""
import json
import logging
import math
import socket
import time
from datetime import datetime, timezone


def record_tick(scheduler):
    db_url = scheduler.app.conf.get("celery_manager_db_url")
    if not db_url:
        return None
    try:
        interval = max(5, float(scheduler.app.conf.get("celery_manager_heartbeat_interval") or 30))
        if not math.isfinite(interval):
            interval = 30
    except (TypeError, ValueError):
        interval = 30
    now = time.monotonic()
    if now - getattr(scheduler, "_cm_last_heartbeat", 0) < interval:
        return interval
    scheduler._cm_last_heartbeat = now
    try:
        from celery_manager.db.models import AppSetting
        from celery_manager.db.session import create_db_engine, get_session_factory
        if getattr(scheduler, "_cm_health_sessions", None) is None:
            engine = create_db_engine(db_url, db_schema=scheduler.app.conf.get("celery_manager_db_schema"))
            scheduler._cm_health_engine = engine
            scheduler._cm_health_sessions = get_session_factory(engine)
        with scheduler._cm_health_sessions() as session:
            stamp = datetime.now(timezone.utc)
            row = session.get(AppSetting, "beat_last_tick")
            if row is None:
                row = AppSetting(key="beat_last_tick")
                session.add(row)
            row.value = json.dumps({"at": stamp.isoformat(), "hostname": socket.gethostname(),
                                    "scheduler": type(scheduler).__name__})
            row.updated_at, row.updated_by = stamp, "beat"
            session.commit()
    except Exception:
        logging.getLogger(__name__).warning("celery-manager: could not record beat heartbeat", exc_info=True)
    return interval
