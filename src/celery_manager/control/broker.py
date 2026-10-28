"""Broker health: can we reach the broker, how fast, and is it in trouble?

`broker_health` answers with one dict tagged by transport. What it can say
depends on what the broker lets a client see:

- **Redis** (including Sentinel): `INFO` exposes memory, clients, evictions and
  persistence. The one that matters most is the eviction policy. With any
  `maxmemory-policy` other than `noeviction`, Redis deletes keys when it
  runs out of memory, and a queued task is just a key in a list: it can be
  evicted and is never seen again. That is flagged as a warning.
- **RabbitMQ over AMQP**: the server properties sent in the handshake
  (product, version). Memory and disk alarms are only visible through the
  management API, which is a separate integration; the answer says so
  (`management: "not configured"`) instead of implying all is well.
- **Anything else**: reachable or not, and "details not available".

Like the queue probe it never raises: every failure becomes
`{"reachable": false, "error": ...}`, and the whole call runs under the same
hard deadline, so a broker that accepts the connection and then goes silent
cannot pin an HTTP request or a metrics scrape.

The broker URL is never returned with its password: only kombu's own masked
form, and error text has any credential scrubbed out of it.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from urllib.parse import unquote

from celery import Celery

from celery_manager.control.queues import (
    BROKER_DEADLINE,
    QueueBrokerError,
    _open_connection,
    _run_with_deadline,
    _transport_name,
)

_MEMORY_WARN_NOTE = (
    "Redis may delete queued tasks to make room when memory runs out. "
    "Set maxmemory-policy to noeviction so a full Redis refuses new tasks instead of losing queued ones."
)


def _mask_secrets(text: str, conn) -> str:
    """`text` with the connection's password removed.

    Exception messages from drivers usually name the host only, but a few echo
    the URL; scrubbing is cheap insurance against putting a credential in an
    API response, a log line or an alert message.
    """
    if not text:
        return text
    secrets = set()
    for attr in ("password", "userid"):
        value = getattr(conn, attr, None)
        if isinstance(value, str) and len(value) >= 3:
            secrets.add(value)
            secrets.add(unquote(value))
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def _iso_from_epoch(value) -> str | None:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def _int(info: dict, key: str) -> int | None:
    try:
        return int(info[key])
    except (KeyError, TypeError, ValueError):
        return None


def redis_details(info: dict) -> dict:
    """Shape a Redis `INFO` reply (already a dict) into our fields + warnings.

    A field the server omits (old versions, managed services that hide some)
    is None rather than an error."""
    used = _int(info, "used_memory")
    maxmemory = _int(info, "maxmemory") or 0
    policy = info.get("maxmemory_policy")
    evicted = _int(info, "evicted_keys")

    warnings: list[str] = []
    if policy is not None and policy != "noeviction":
        note = _MEMORY_WARN_NOTE
        if maxmemory == 0:
            # Without a limit Redis never reaches the point of evicting, so
            # say that, rather than raising a false alarm about today.
            note += " No memory limit is set right now, so nothing is being evicted today."
        warnings.append(f"The eviction policy is {policy}, not noeviction. {note}")
    if evicted:
        warnings.append(
            f"Redis has evicted {evicted:,} keys since it started. If any of them were queued tasks, those tasks are lost."
        )
    rejected = _int(info, "rejected_connections")
    if rejected:
        warnings.append(f"Redis has refused {rejected:,} connections (maxclients reached) since it started.")

    rdb_save_status = info.get("rdb_last_bgsave_status")
    aof_enabled = str(info.get("aof_enabled", "0")) == "1"
    if rdb_save_status not in (None, "ok"):
        warnings.append("The last RDB snapshot failed, so a restart could lose recent data.")
    if aof_enabled and info.get("aof_last_write_status") not in (None, "ok"):
        warnings.append("The last append-only-file write failed.")

    return {
        "version": info.get("redis_version"),
        "used_memory": used,
        # 0 means Redis has no limit, so there is no ratio to report.
        "maxmemory": maxmemory,
        "memory_ratio": round(used / maxmemory, 4) if used is not None and maxmemory > 0 else None,
        "maxmemory_policy": policy,
        "connected_clients": _int(info, "connected_clients"),
        "blocked_clients": _int(info, "blocked_clients"),
        "evicted_keys": evicted,
        "rejected_connections": rejected,
        "uptime_seconds": _int(info, "uptime_in_seconds"),
        "persistence": {
            "rdb_last_save_status": rdb_save_status,
            "rdb_last_save_at": _iso_from_epoch(info.get("rdb_last_save_time")),
            "rdb_changes_since_save": _int(info, "rdb_changes_since_last_save"),
            "aof_enabled": aof_enabled,
            "aof_last_write_status": info.get("aof_last_write_status") if aof_enabled else None,
        },
        "warnings": warnings,
    }


def _text(value) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return value if isinstance(value, str) else None


def amqp_details(conn) -> dict:
    props = getattr(getattr(conn, "connection", None), "server_properties", None) or {}
    return {
        "product": _text(props.get("product")),
        "version": _text(props.get("version")),
        "cluster_name": _text(props.get("cluster_name")),
        # Alarms (memory, disk) are only exposed by the management API.
        "management": "not configured",
        "warnings": [],
    }


def _probe(celery_app: Celery) -> dict:
    """One connection attempt. Raises on failure; `broker_health` classifies."""
    started = time.monotonic()
    conn = _open_connection(celery_app)
    out: dict = {"url": conn.as_uri()}  # kombu masks the password
    try:
        transport = _transport_name(conn)
        out["transport"] = transport
        out["sentinel"] = type(getattr(conn, "transport", None)).__name__ == "SentinelTransport"
        details = None
        if transport == "redis":
            # kombu's Redis channel pings on creation, so getting the channel
            # proves the server answers; the same client then reads INFO.
            client = conn.default_channel.client
            client.ping()
            out["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
            details = redis_details(client.info())
        elif transport == "amqp":
            out["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
            details = amqp_details(conn)
        else:
            out["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
        out["details"] = details
        return out
    finally:
        try:
            conn.release()
        except Exception:  # noqa: BLE001, S110 — releasing a broken connection is best-effort
            pass


def _configured_transport(celery_app: Celery) -> str | None:
    """The transport named by the broker URL, for a failed probe that never got far enough to ask."""
    try:
        conn = celery_app.connection_for_read()
        name = _transport_name(conn)
        conn.release()
    except Exception:  # noqa: BLE001 — a malformed URL: "unknown" is the honest answer
        return None
    return None if name == "unknown" else name


def broker_health(celery_app: Celery, timeout: float | None = None) -> dict:
    """The broker's health. Never raises.

    Keys: `transport`, `reachable`, `latency_ms`, `url` (credentials masked),
    `error`, `checked_at`, `sentinel`, and `details`: transport-specific
    fields, or None with `details_note` saying why not. `timeout` is the
    overall deadline in seconds (default: the queue probe's).
    """
    deadline = BROKER_DEADLINE if timeout is None else timeout
    result: dict = {
        "transport": None,
        "reachable": False,
        "latency_ms": None,
        "url": None,
        "error": None,
        "sentinel": False,
        "details": None,
        "details_note": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        # A short connect timeout keeps a dead host from eating the deadline
        # in kombu's retry loop; the deadline covers a host that answers and
        # then goes quiet.
        probe = _run_with_deadline(_probe, celery_app, deadline=deadline)
    except QueueBrokerError as exc:
        result["error"] = str(exc)
        result["transport"] = _configured_transport(celery_app)
        result["url"] = _safe_url(celery_app)
        return result
    except Exception as exc:  # noqa: BLE001 — unreachable, auth failure, bad URL, INFO refused: all "not healthy"
        result["error"] = _scrub_error(celery_app, exc)
        result["transport"] = _configured_transport(celery_app)
        result["url"] = _safe_url(celery_app)
        return result

    result.update(
        transport=probe["transport"],
        reachable=True,
        latency_ms=probe["latency_ms"],
        url=probe["url"],
        sentinel=probe["sentinel"],
        details=probe["details"],
    )
    if probe["details"] is None:
        result["details_note"] = f"details not available for {probe['transport']}"
    return result


def _safe_url(celery_app: Celery) -> str | None:
    try:
        return celery_app.connection_for_read().as_uri()
    except Exception:  # noqa: BLE001 — a URL kombu can't parse is not worth echoing
        return None


def _scrub_error(celery_app: Celery, exc: Exception) -> str:
    text = str(exc) or type(exc).__name__
    try:
        text = _mask_secrets(text, celery_app.connection_for_read())
    except Exception:  # noqa: BLE001, S110 — scrubbing is best-effort on top of an error we already have
        pass
    return f"could not reach the broker: {text}"
