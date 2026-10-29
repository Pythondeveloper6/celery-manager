"""Throttled durable beat identity and optional external dead-man pings."""
import hashlib
import json
import logging
import math
import socket
import threading
import time
from datetime import datetime, timezone
from http.client import HTTPException as HTTPClientException
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

OWNER_PREFIX = "beat_owner:"
logger = logging.getLogger(__name__)


def _bounded(value, default, minimum, maximum):
    try:
        result = float(value)
        return min(maximum, max(minimum, result)) if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def owner_key(owner_id):
    # AppSetting.key is VARCHAR(64), including this namespace prefix.
    return OWNER_PREFIX + hashlib.sha256(owner_id.encode()).hexdigest()[:64 - len(OWNER_PREFIX)]


def owner_identity(scheduler):
    explicit = scheduler.app.conf.get("cm_beat_owner_id")
    if explicit:
        return str(explicit)[:500]
    return f"{socket.gethostname()}:{scheduler.app.main}:{type(scheduler).__name__}"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _send_ping(url, timeout):
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Invalid heartbeat URL")
        request = Request(url, headers={"User-Agent": "celery-manager-beat"}, method="GET")
        # Redirects can expose a secret URL to another host. Do not follow them.
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise ValueError("Unsuccessful heartbeat response")
    except (OSError, ValueError, HTTPClientException):
        # URLs often contain monitor tokens. Never log the URL or exception.
        logger.warning("celery-manager: external beat heartbeat failed")


def _maybe_ping(scheduler, now):
    url = scheduler.app.conf.get("cm_beat_healthcheck_url")
    if not url:
        return None
    interval = _bounded(scheduler.app.conf.get("cm_beat_healthcheck_interval"), 30, 5, 86400)
    previous = getattr(scheduler, "_cm_ping_last_attempt", None)
    if previous is not None and now - previous < interval:
        return interval
    thread = getattr(scheduler, "_cm_ping_thread", None)
    if thread is not None and thread.is_alive():
        return interval
    scheduler._cm_ping_last_attempt = now
    timeout = _bounded(scheduler.app.conf.get("cm_beat_healthcheck_timeout"), 3, 0.1, 10)
    try:
        thread = threading.Thread(target=_send_ping, args=(str(url), timeout), name="cm-beat-ping", daemon=True)
        scheduler._cm_ping_thread = thread
        thread.start()
    except RuntimeError:
        logger.warning("celery-manager: could not start external beat heartbeat")
    return interval


def record_tick(scheduler):
    now = time.monotonic()
    ping_interval = _maybe_ping(scheduler, now)
    db_url = scheduler.app.conf.get("celery_manager_db_url")
    if not db_url:
        return ping_interval
    interval = _bounded(scheduler.app.conf.get("celery_manager_heartbeat_interval"), 30, 5, 86400)
    wake_interval = min(interval, ping_interval) if ping_interval else interval
    previous = getattr(scheduler, "_cm_last_heartbeat", None)
    if previous is not None and now - previous < interval:
        return wake_interval
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
            owner = owner_identity(scheduler)
            data = json.dumps({"at": stamp.isoformat(), "hostname": socket.gethostname(),
                               "scheduler": type(scheduler).__name__, "owner_id": owner})
            key = owner_key(owner)
            for setting_key in ("beat_last_tick", key):
                row = session.get(AppSetting, setting_key)
                if row is None:
                    row = AppSetting(key=setting_key)
                    session.add(row)
                row.value, row.updated_at, row.updated_by = data, stamp, "beat"
            session.commit()
    except Exception:
        logger.warning("celery-manager: could not record beat heartbeat", exc_info=True)
    return wake_interval
