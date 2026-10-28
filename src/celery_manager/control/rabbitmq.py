"""RabbitMQ beyond queue depth: the management API, and dead-letter queues.

Two channels to the broker, each for what only it can do:

- **The management HTTP API** (`rabbitmq_management_url`) for everything a
  passive `queue_declare` can't say: unacknowledged messages, publish and
  deliver rates, queue type and arguments, node memory/disk alarms, and
  which queues dead-letter into which. Read with a tiny stdlib client
  (urllib): httpx is only a dev dependency, and four GETs don't justify a
  new runtime one. It also browses a dead-letter queue (`POST .../get` with
  `ack_requeue_true`), which is the one way to look at messages without
  consuming them for good.
- **AMQP** (kombu, the app's own broker connection) for requeue. The
  management API can't publish *and* ack atomically, but AMQP with
  publisher confirms can do the one ordering that never loses a message:
  take it off the DLQ unacked, publish the copy, wait for the broker's
  confirm (and no `basic.return`), and only then ack the original.

Credentials live only in `CeleryManagerConfig`. They are sent as a Basic
auth header, never put in a URL we build, never returned by the API, and
never part of an error message: errors name the status and what to check,
not the request.

Every management call runs on its own small thread pool with a hard
deadline, the pattern `control.queues` uses for the broker: a management
API that accepts the connection and then stalls must not pin an HTTP
request of ours (the Queues page polls every five seconds). A separate pool
from the broker probe's, so a slow management API can't starve queue depth.
"""

from __future__ import annotations

import base64
import json
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit

from amqp import Message as AmqpMessage
from celery import Celery

from celery_manager.control.audit import record as record_audit

# Per-queue columns asked of `GET /api/queues/{vhost}`. The full objects
# carry per-node backing-queue internals and garbage-collection details that
# can run to kilobytes per queue; the columns filter keeps a 5-second poll
# cheap on a broker with hundreds of queues.
QUEUE_COLUMNS = (
    "name", "type", "arguments", "effective_policy_definition", "consumers", "messages",
    "messages_ready", "messages_unacknowledged", "message_stats", "idle_since", "state", "node",
)
NODE_COLUMNS = (
    "name", "running", "mem_used", "mem_limit", "mem_alarm", "disk_free", "disk_free_limit",
    "disk_free_alarm", "fd_used", "fd_total", "sockets_used", "sockets_total", "uptime",
)

DEFAULT_DLQ_PATTERNS = ("*.dlq", "*.dead", "dlq.*")

# Browsing takes messages off the DLQ and puts them straight back
# (`ack_requeue_true`), so each look has side effects; keep it to one screen.
BROWSE_MAX = 20

# Requeue: at most this many messages per request, and this many seconds
# overall. A request that hits either stops cleanly between messages and
# reports what it moved; the rest stay where they were.
REQUEUE_MAX = 1000
REQUEUE_DEFAULT = 100
REQUEUE_BUDGET_SECONDS = 30.0
# How many messages a "selected messages" requeue looks through for the
# ones asked for. Skipped messages are held unacked meanwhile and handed
# back at the end, so this bounds what one request holds.
REQUEUE_SCAN_MAX = 1000

# Loop protection: a message that has died more often than this is refused
# unless the request says `force`. Counted from RabbitMQ's x-death counts and
# from our own header (below), whichever says more.
MAX_DEATHS = 3
# Our own count of requeues, carried on the message. RabbitMQ 4 no longer
# trusts x-death headers that a client publishes (it starts counting again),
# so this is what still catches a message going round in circles there.
REQUEUE_HEADER = "x-celery-manager-requeues"

CONNECT_TIMEOUT = 2.0

# Transports that speak to RabbitMQ (kombu reports both py-amqp and
# librabbitmq as "amqp").
_RABBIT_DRIVERS = frozenset({"amqp"})

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cm-rabbitmq")


class RabbitMQNotConfigured(Exception):
    """Management isn't set up, or the broker isn't RabbitMQ. The text says which."""


class RabbitMQError(RuntimeError):
    """The management API (or the broker) could not be asked, or refused."""


class RabbitMQNotFound(LookupError):
    """The queue doesn't exist, or isn't a dead-letter queue."""


# --- configuration -----------------------------------------------------------------


def broker_transport(celery_app: Celery) -> str:
    """The broker's kombu driver ("amqp", "redis", ...). Builds the
    connection object only; kombu connects lazily, so nothing goes over the
    network here."""
    conn = celery_app.connection_for_read()
    try:
        return str(getattr(conn.transport, "driver_type", "") or "unknown")
    finally:
        conn.release()


def broker_vhost(celery_app: Celery) -> str:
    conn = celery_app.connection_for_read()
    try:
        return conn.virtual_host or "/"
    finally:
        conn.release()


def not_configured_reason(config) -> str | None:
    """Why the RabbitMQ features are off, or None when they're on."""
    try:
        transport = broker_transport(config.celery_app)
    except Exception as exc:  # noqa: BLE001 — an unparsable broker URL is just "can't tell"
        return f"the broker URL could not be read: {exc}"
    if transport not in _RABBIT_DRIVERS:
        return f"the broker is {transport}, not RabbitMQ"
    if not config.rabbitmq_management_url:
        return "rabbitmq_management_url is not set"
    return None


def _not_configured(reason: str) -> dict:
    return {"configured": False, "reason": reason}


# --- the management API client ---------------------------------------------------------


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    """Never follow a redirect. Following one would resend the Basic auth
    header to wherever it points; a management URL that redirects is a
    misconfiguration to report, not to work around."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: PLR0913 — urllib's signature
        return None


class ManagementClient:
    """A few JSON calls against the RabbitMQ management API.

    `deadline` (a `time.monotonic()` value) bounds every call made through
    one client: each socket timeout is the smaller of the configured timeout
    and the time left, so an operation of several calls can't add up past it.
    """

    def __init__(self, base_url: str, username: str | None, password: str | None, *,
                 verify_tls: bool = True, timeout: float = 3.0, deadline: float | None = None):
        parts = urlsplit(base_url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise RabbitMQNotConfigured("rabbitmq_management_url must be an http(s) URL like http://rabbit:15672")
        # Credentials in the URL are taken out of it: they would otherwise end
        # up in every request line we could ever log.
        if parts.username and not username:
            username, password = unquote(parts.username), unquote(parts.password or "")
        netloc = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
        if parts.port:
            netloc += f":{parts.port}"
        self.base = urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))
        self._auth = None
        if username:
            token = base64.b64encode(f"{username}:{password or ''}".encode()).decode("ascii")
            self._auth = f"Basic {token}"
        self.timeout = timeout
        self.deadline = deadline
        handlers: list = [_NoRedirect()]
        if parts.scheme == "https":
            context = ssl.create_default_context()
            if not verify_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            handlers.append(urlrequest.HTTPSHandler(context=context))
        self._opener = urlrequest.build_opener(*handlers)

    def _timeout(self) -> float:
        if self.deadline is None:
            return self.timeout
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise RabbitMQError(f"the management API did not answer within {self.timeout:g}s")
        return min(self.timeout, left)

    def request(self, method: str, path: str, query: dict | None = None, body: dict | None = None,
                ok_statuses: tuple[int, ...] = ()) -> object:
        url = self.base + path + (f"?{urlencode(query)}" if query else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urlrequest.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self._auth:
            req.add_header("Authorization", self._auth)
        try:
            with self._opener.open(req, timeout=self._timeout()) as response:
                raw = response.read()
        except urlerror.HTTPError as exc:
            if exc.code in ok_statuses:
                raw = exc.read()
            else:
                raise _http_error(exc, path) from None
        except (TimeoutError, ssl.SSLError, urlerror.URLError, OSError) as exc:
            raise _transport_error(exc, self.timeout) from None
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            raise RabbitMQError("the management API answered with something that isn't JSON; "
                                "check that rabbitmq_management_url points at the management plugin") from None

    def get(self, path: str, query: dict | None = None, ok_statuses: tuple[int, ...] = ()):
        return self.request("GET", path, query=query, ok_statuses=ok_statuses)

    def post(self, path: str, body: dict):
        return self.request("POST", path, body=body)


def _http_error(exc: urlerror.HTTPError, path: str) -> Exception:
    code = exc.code
    if code == 401:
        return RabbitMQError("the management API refused the login (401): check the management credentials "
                             "(rabbitmq_management_username / rabbitmq_management_password)")
    if code == 403:
        return RabbitMQError("the management user isn't allowed to do this (403): it needs the monitoring tag "
                             "to read, and read+write permission on the vhost to browse or requeue")
    if code == 404:
        return RabbitMQNotFound(f"not found on the management API: {unquote(path)}")
    if 300 <= code < 400:
        location = urlsplit(exc.headers.get("Location") or "")
        where = f" to {location.scheme}://{location.netloc}" if location.netloc else ""
        return RabbitMQError(f"the management API redirected{where} ({code}); "
                             "set rabbitmq_management_url to the address it redirects to")
    return RabbitMQError(f"the management API answered {code} {exc.reason}")


def _transport_error(exc: BaseException, timeout: float) -> RabbitMQError:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, TimeoutError) or "timed out" in str(reason):
        return RabbitMQError(f"the management API did not answer within {timeout:g}s")
    if isinstance(reason, ssl.SSLError):
        return RabbitMQError(f"TLS to the management API failed: {reason}; "
                             "set rabbitmq_management_verify_tls=False only for a self-signed test broker")
    return RabbitMQError(f"could not reach the management API: {reason}")


def _client(config, deadline: float | None = None) -> ManagementClient:
    return ManagementClient(
        config.rabbitmq_management_url,
        config.rabbitmq_management_username,
        config.rabbitmq_management_password,
        verify_tls=config.rabbitmq_management_verify_tls,
        timeout=config.rabbitmq_management_timeout_seconds,
        deadline=deadline,
    )


def _with_deadline(config, fn, *args):
    """Run `fn(client, *args)` on the pool, bounded by the configured timeout.

    Each call inside shares one deadline (so several calls can't add up);
    the pool's wait gets a little slack over it so the client's own, clearer
    timeout error usually wins the race.
    """
    reason = not_configured_reason(config)
    if reason:
        raise RabbitMQNotConfigured(reason)
    budget = float(config.rabbitmq_management_timeout_seconds)
    client = _client(config, deadline=time.monotonic() + budget)
    future = _executor.submit(fn, client, *args)
    try:
        return future.result(timeout=budget + 0.5)
    except FutureTimeout:
        raise RabbitMQError(f"the management API did not answer within {budget:g}s") from None


def _vhost_path(config) -> str:
    return quote(broker_vhost(config.celery_app), safe="")


# --- shaping management answers -----------------------------------------------------------


def _rate(stats: dict | None, key: str) -> float | None:
    details = (stats or {}).get(f"{key}_details")
    if isinstance(details, dict) and isinstance(details.get("rate"), (int, float)):
        return float(details["rate"])
    return None


def _queue_type(raw: dict) -> str:
    args = raw.get("arguments") or {}
    return str(raw.get("type") or args.get("x-queue-type") or "classic")


def _dead_letter_target(raw: dict) -> tuple[str | None, str | None]:
    """(exchange, routing key) a queue dead-letters to, or (None, None).

    A queue argument wins over a policy, as in RabbitMQ itself."""
    args = raw.get("arguments") or {}
    policy = raw.get("effective_policy_definition") or {}
    if not isinstance(policy, dict):
        policy = {}
    exchange = args.get("x-dead-letter-exchange", policy.get("dead-letter-exchange"))
    routing_key = args.get("x-dead-letter-routing-key", policy.get("dead-letter-routing-key"))
    if exchange is None:
        return None, None
    return str(exchange), (str(routing_key) if routing_key is not None else None)


def queue_entry(raw: dict) -> dict:
    """One management queue object, reduced to what the dashboard shows."""
    stats = raw.get("message_stats") or {}
    args = raw.get("arguments") or {}
    dlx, dlrk = _dead_letter_target(raw)
    priority = args.get("x-max-priority")
    return {
        "type": _queue_type(raw),
        "state": raw.get("state"),
        "node": raw.get("node"),
        "consumers": raw.get("consumers"),
        "messages": raw.get("messages"),
        "messages_ready": raw.get("messages_ready"),
        "messages_unacknowledged": raw.get("messages_unacknowledged"),
        "publish_rate": _rate(stats, "publish"),
        # deliver_get covers both consumers (deliver) and polling (basic.get).
        "deliver_rate": _rate(stats, "deliver_get"),
        "max_priority": int(priority) if isinstance(priority, (int, float)) else None,
        "dead_letter_exchange": dlx,
        "dead_letter_routing_key": dlrk,
        "idle_since": raw.get("idle_since"),
    }


def node_entry(raw: dict) -> dict:
    return {key: raw.get(key) for key in NODE_COLUMNS}


def _fetch_queues(client: ManagementClient, vhost: str) -> list[dict]:
    rows = client.get(f"/api/queues/{vhost}", {"columns": ",".join(QUEUE_COLUMNS)})
    return [r for r in rows or [] if isinstance(r, dict) and r.get("name")]


def _fetch_nodes(client: ManagementClient) -> list[dict]:
    rows = client.get("/api/nodes", {"columns": ",".join(NODE_COLUMNS)})
    return [node_entry(r) for r in rows or [] if isinstance(r, dict)]


def fetch_queue(config, name: str) -> dict:
    """`GET /api/queues/{vhost}/{name}`, shaped by `queue_entry`."""
    def run(client):
        raw = client.get(f"/api/queues/{_vhost_path(config)}/{quote(name, safe='')}",
                         {"columns": ",".join(QUEUE_COLUMNS)})
        return {"name": name, **queue_entry(raw or {})}
    return _with_deadline(config, run)


# --- the Queues page ---------------------------------------------------------------------


def annotate_queues(config, listing: dict) -> dict:
    """Add each queue's management details to a `list_queues` result.

    Sets `listing["rabbitmq"]` to `{configured, reason, error, is_rabbitmq}`
    and, only when configured, each queue's `rabbitmq` to its `queue_entry`
    (None when the management API doesn't know the queue or can't be
    asked). Never raises: the depth numbers are worth showing whatever the
    management API is doing.
    """
    # The listing already knows the transport; reuse it rather than build
    # another broker connection object on every 5-second refresh.
    transport = (listing.get("broker") or {}).get("transport")
    if transport and transport not in _RABBIT_DRIVERS:
        reason = f"the broker is {transport}, not RabbitMQ"
    elif not config.rabbitmq_management_url:
        reason = "rabbitmq_management_url is not set"
    else:
        reason = not_configured_reason(config)
    meta = {"configured": reason is None, "reason": reason, "error": None,
            "is_rabbitmq": transport in _RABBIT_DRIVERS if transport else None}
    listing["rabbitmq"] = meta
    if reason is not None:
        # Queues keep exactly the shape they have without RabbitMQ.
        return listing
    by_name: dict[str, dict] = {}
    try:
        raw = _with_deadline(config, lambda client: _fetch_queues(client, _vhost_path(config)))
        by_name = {r["name"]: queue_entry(r) for r in raw}
    except (RabbitMQError, RabbitMQNotFound, RabbitMQNotConfigured) as exc:
        meta["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 — a surprise answer must not break the Queues page
        meta["error"] = f"could not read the management API: {exc}"
    for queue in listing.get("queues") or []:
        queue["rabbitmq"] = by_name.get(queue["name"])
    return listing


def overview(config) -> dict:
    """Cluster version, node resources and alarms, for the RabbitMQ panel."""
    reason = not_configured_reason(config)
    if reason:
        return _not_configured(reason)

    def run(client):
        info = client.get("/api/overview", {"columns": "rabbitmq_version,management_version,cluster_name,object_totals,queue_totals,message_stats"}) or {}
        nodes = _fetch_nodes(client)
        # 200 {"status":"ok"} or 503 {"status":"failed","reason":...}: both are answers.
        alarms = client.get("/api/health/checks/alarms", ok_statuses=(503,)) or {}
        return info, nodes, alarms

    info, nodes, alarms = _with_deadline(config, run)
    totals = info.get("queue_totals") or {}
    return {
        "configured": True,
        "vhost": broker_vhost(config.celery_app),
        "rabbitmq_version": info.get("rabbitmq_version"),
        "management_version": info.get("management_version"),
        "cluster_name": info.get("cluster_name"),
        "messages": totals.get("messages"),
        "messages_ready": totals.get("messages_ready"),
        "messages_unacknowledged": totals.get("messages_unacknowledged"),
        "publish_rate": _rate(info.get("message_stats"), "publish"),
        "deliver_rate": _rate(info.get("message_stats"), "deliver_get"),
        "nodes": nodes,
        "alarms": {
            "ok": alarms.get("status") == "ok",
            "reason": alarms.get("reason"),
            "nodes": sorted({n["name"] for n in nodes if n.get("mem_alarm") or n.get("disk_free_alarm")}),
        },
    }


def nodes_for_alerts(config) -> list[dict]:
    """Every node with its alarm flags; raises when that can't be known."""
    reason = not_configured_reason(config)
    if reason:
        raise RabbitMQNotConfigured(reason)
    return _with_deadline(config, _fetch_nodes)


def queue_stats(config) -> tuple[list[dict], list[dict]]:
    """(queues, nodes) for the metrics: one listing call and one nodes call."""
    def run(client):
        queues = [{"name": r["name"], **queue_entry(r)} for r in _fetch_queues(client, _vhost_path(config))]
        return queues, _fetch_nodes(client)
    return _with_deadline(config, run)


# --- dead-letter queues -------------------------------------------------------------------


def _topic_matches(pattern: str, key: str) -> bool:
    """AMQP topic matching: `*` is exactly one word, `#` zero or more."""
    def match(p: list[str], k: list[str]) -> bool:
        if not p:
            return not k
        if p[0] == "#":
            return any(match(p[1:], k[i:]) for i in range(len(k) + 1))
        if not k:
            return False
        return (p[0] == "*" or p[0] == k[0]) and match(p[1:], k[1:])
    return match(pattern.split("."), key.split("."))


def _binding_receives(exchange_type: str, binding_key: str, routing_key: str | None) -> bool:
    """Whether a binding would receive a dead-lettered message. With no
    dead-letter routing key the message keeps its own key, which we can't
    know in advance, so every binding of a direct or topic exchange might."""
    if exchange_type in ("fanout", "headers") or routing_key is None:
        return True
    if exchange_type == "topic":
        return _topic_matches(binding_key, routing_key)
    return binding_key == routing_key


def _detect_dlqs(client: ManagementClient, vhost: str, patterns) -> dict:
    queues = _fetch_queues(client, vhost)
    names = {q["name"] for q in queues}
    found: dict[str, dict] = {}
    warnings: list[str] = []

    def mark(name: str, how: str, source: str | None = None):
        entry = found.setdefault(name, {"name": name, "matched_by": [], "dead_letter_sources": []})
        if how not in entry["matched_by"]:
            entry["matched_by"].append(how)
        if source and source not in entry["dead_letter_sources"]:
            entry["dead_letter_sources"].append(source)

    # Queues dead-lettering through each exchange, so each exchange's
    # bindings are read once however many queues use it.
    by_exchange: dict[str, list[tuple[str, str | None]]] = {}
    for q in queues:
        exchange, routing_key = _dead_letter_target(q)
        if exchange is None:
            continue
        if exchange == "":
            # The default exchange routes by queue name.
            if routing_key in names:
                mark(routing_key, "dead_letter_target", q["name"])
            elif routing_key:
                warnings.append(f"{q['name']} dead-letters to queue {routing_key!r}, which doesn't exist: "
                                "its dead letters are dropped")
            continue
        by_exchange.setdefault(exchange, []).append((q["name"], routing_key))

    for exchange, sources in list(by_exchange.items())[:20]:
        try:
            info = client.get(f"/api/exchanges/{vhost}/{quote(exchange, safe='')}", {"columns": "name,type"}) or {}
            bindings = client.get(f"/api/exchanges/{vhost}/{quote(exchange, safe='')}/bindings/source") or []
        except RabbitMQNotFound:
            for source, _ in sources:
                warnings.append(f"{source} dead-letters to exchange {exchange!r}, which doesn't exist: "
                                "its dead letters are dropped")
            continue
        exchange_type = str(info.get("type") or "direct")
        for source, routing_key in sources:
            targets = [
                b["destination"] for b in bindings
                if isinstance(b, dict) and b.get("destination_type") == "queue"
                and _binding_receives(exchange_type, str(b.get("routing_key") or ""), routing_key)
            ]
            if not targets:
                warnings.append(f"{source} dead-letters to exchange {exchange!r}, but no queue is bound to it"
                                + (f" with key {routing_key!r}" if routing_key else "")
                                + ": its dead letters are dropped")
            for target in targets:
                mark(target, "dead_letter_target", source)

    for q in queues:
        if any(fnmatchcase(q["name"], p) for p in patterns):
            mark(q["name"], "name_pattern")

    by_name = {q["name"]: q for q in queues}
    items = []
    for name in sorted(found):
        raw = by_name.get(name, {})
        items.append({
            **found[name],
            "messages": raw.get("messages"),
            "messages_ready": raw.get("messages_ready"),
            "consumers": raw.get("consumers"),
            "type": _queue_type(raw) if raw else None,
        })
    return {"queues": items, "warnings": warnings}


def list_dlqs(config) -> dict:
    """Dead-letter queues: targets of some queue's dead-letter exchange and
    routing key (from queue arguments or a policy, via bindings), plus queues
    whose name matches `rabbitmq_dlq_patterns`."""
    reason = not_configured_reason(config)
    if reason:
        return _not_configured(reason)
    patterns = tuple(config.rabbitmq_dlq_patterns or ())
    result = _with_deadline(config, lambda client: _detect_dlqs(client, _vhost_path(config), patterns))
    return {"configured": True, "patterns": list(patterns), **result}


def _require_dlq(config, client: ManagementClient, name: str) -> dict:
    """The DLQ entry for `name`, or RabbitMQNotFound. Browse and requeue
    work on detected dead-letter queues only: on a live work queue, both
    would reorder (or move) real work."""
    detected = _detect_dlqs(client, _vhost_path(config), tuple(config.rabbitmq_dlq_patterns or ()))
    for entry in detected["queues"]:
        if entry["name"] == name:
            return entry
    raise RabbitMQNotFound(f"{name!r} is not a dead-letter queue (see GET /api/rabbitmq/dlqs)")


def _iso_time(value) -> str | None:
    if isinstance(value, datetime):
        value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return str(value) if value is not None else None


def x_death_entries(headers: dict | None) -> list[dict]:
    """RabbitMQ's `x-death` header, most recent dead-lettering first."""
    entries = []
    for raw in (headers or {}).get("x-death") or []:
        if not isinstance(raw, dict):
            continue
        keys = raw.get("routing-keys") or []
        entries.append({
            "reason": raw.get("reason"),
            "queue": raw.get("queue"),
            "exchange": raw.get("exchange"),
            "routing_keys": [k.decode() if isinstance(k, bytes) else str(k) for k in keys],
            "count": int(raw.get("count") or 0),
            "time": _iso_time(raw.get("time")),
        })
    return entries


def death_count(headers: dict | None) -> int:
    """How often this message has died, for loop protection: the larger of
    RabbitMQ's x-death counts and our own requeue count (plus the death that
    put it in the DLQ now)."""
    headers = headers or {}
    deaths = sum(e["count"] for e in x_death_entries(headers))
    try:
        requeues = int(headers.get(REQUEUE_HEADER) or 0)
    except (TypeError, ValueError):
        requeues = 0
    return max(deaths, requeues + 1)


def message_key(properties: dict | None, headers: dict | None) -> str | None:
    """What identifies a message across a browse and a later requeue: the
    Celery task id (protocol 2 header), else the AMQP message id."""
    headers = headers or {}
    properties = properties or {}
    key = headers.get("id") or properties.get("message_id") or properties.get("correlation_id")
    return str(key) if key else None


def _decode_body(payload, encoding: str | None, content_type: str | None):
    if payload is None:
        return None
    if encoding == "base64":
        try:
            payload = base64.b64decode(payload)
        except ValueError:
            return None
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if content_type and "json" not in content_type:
        return None
    try:
        return json.loads(payload)
    except (TypeError, ValueError):
        return None


def _arguments(headers: dict, body) -> tuple[str | None, str | None]:
    """(args, kwargs) as text. Celery's own `argsrepr`/`kwargsrepr` headers
    first: they are what the app chose to show (and may already hide
    secrets). Else the JSON body, protocol 2 (`[args, kwargs, embed]`) or 1."""
    if "argsrepr" in headers or "kwargsrepr" in headers:
        return headers.get("argsrepr"), headers.get("kwargsrepr")
    if isinstance(body, list) and len(body) >= 2:
        return json.dumps(body[0]), json.dumps(body[1])
    if isinstance(body, dict) and ("args" in body or "kwargs" in body):
        return json.dumps(body.get("args")), json.dumps(body.get("kwargs"))
    return None, None


def describe_message(item: dict, reveal_args: bool) -> dict:
    """One message from the management API's `get`, for the dashboard.

    Arguments are masked unless `reveal_args`: a dead-letter queue is often
    looked at while sharing a screen in an incident, so they show only when
    someone asks for them, the same policy as the dashboard's task view.
    """
    properties = item.get("properties") or {}
    headers = properties.get("headers") or {}
    body = _decode_body(item.get("payload"), item.get("payload_encoding"), properties.get("content_type"))
    task = headers.get("task") or (body.get("task") if isinstance(body, dict) else None)
    task_id = headers.get("id") or (body.get("id") if isinstance(body, dict) else None)
    args, kwargs = _arguments(headers, body)
    deaths = x_death_entries(headers)
    return {
        "key": message_key(properties, headers),
        "task_name": task,
        "task_id": task_id,
        "args": args if reveal_args else None,
        "kwargs": kwargs if reveal_args else None,
        "args_masked": not reveal_args and (args is not None or kwargs is not None),
        "routing_key": item.get("routing_key"),
        "exchange": item.get("exchange"),
        "redelivered": item.get("redelivered"),
        "content_type": properties.get("content_type"),
        "payload_bytes": item.get("payload_bytes"),
        "x_death": deaths,
        "first_death_reason": headers.get("x-first-death-reason"),
        "first_death_queue": headers.get("x-first-death-queue"),
        "death_count": death_count(headers),
        "requeued_by_celery_manager": int(headers.get(REQUEUE_HEADER) or 0),
    }


def browse_dlq(config, name: str, count: int = BROWSE_MAX, reveal_args: bool = False) -> dict:
    """Up to `count` (≤ BROWSE_MAX) messages from the head of a DLQ.

    Uses the management API's `get` with `ack_requeue_true`: RabbitMQ hands
    each message out and puts it back. That is the only way to read a queue
    without draining it, and it has side effects: the messages come back
    marked redelivered, may change position, and a consumer on the DLQ
    could take one meanwhile. Hence `approximate: true` in the answer.
    """
    reason = not_configured_reason(config)
    if reason:
        return _not_configured(reason)
    count = max(1, min(int(count), BROWSE_MAX))

    def run(client):
        dlq = _require_dlq(config, client, name)
        items = client.post(
            f"/api/queues/{_vhost_path(config)}/{quote(name, safe='')}/get",
            {"count": count, "ackmode": "ack_requeue_true", "encoding": "auto", "truncate": 50_000},
        ) or []
        return dlq, items

    dlq, items = _with_deadline(config, run)
    return {
        "configured": True,
        "queue": name,
        "messages_ready": dlq.get("messages_ready"),
        "approximate": True,
        "limit": count,
        "args_revealed": reveal_args,
        "messages": [describe_message(item, reveal_args) for item in items if isinstance(item, dict)],
    }


# --- requeue over AMQP ------------------------------------------------------------------


def _destination(headers: dict) -> tuple[str, str] | None:
    """Where a dead-lettered message was originally published: the exchange
    and first routing key of its most recent x-death entry."""
    deaths = x_death_entries(headers)
    if not deaths:
        return None
    first = deaths[0]
    if first["exchange"] is None or not first["routing_keys"]:
        return None
    return str(first["exchange"]), first["routing_keys"][0]


class _Returned(Exception):
    pass


def requeue_on_channel(channel, name: str, *, keys: set[str] | None, limit: int, force: bool,
                       max_deaths: int = MAX_DEATHS, confirm_timeout: float = 5.0,
                       budget_seconds: float = REQUEUE_BUDGET_SECONDS, clock=time.monotonic) -> dict:
    """Move messages from DLQ `name` back to where they came from.

    `channel` is a py-amqp channel (a kombu pyamqp channel is one). For each
    message, in order: `basic_get` without auto-ack, publish a copy to the
    original exchange and routing key (mandatory, publisher confirms), and
    only once the broker has confirmed it, with no `basic.return`, ack the
    original. A failure at any step stops the run with the failing message
    put back (`basic_reject(requeue=True)`) and every later one never taken,
    so the worst case is a duplicate (confirmed but the ack lost), never a
    loss.

    `keys`: only these messages (see `message_key`); None means "all",
    up to `limit`. Messages passed over (not selected, refused by loop
    protection, nowhere to send) are held unacked until the end and then
    handed back, so the scan never sees the same message twice.

    Kept x-death: the copy carries the original headers, x-death included,
    plus REQUEUE_HEADER incremented. Keeping x-death keeps the history
    visible if it dies again, and lets RabbitMQ 3.x keep counting deaths;
    our own counter does that job on RabbitMQ 4, which resets x-death on
    messages a client publishes.
    """
    started = clock()
    moved: list[dict] = []
    refused: list[dict] = []
    held: list[int] = []
    returned: list[str] = []
    error: str | None = None
    stopped = None
    scanned = 0
    wanted = set(keys) if keys is not None else None

    def on_return(exc, exchange, routing_key, message):
        returned.append(f"{exc}")

    channel.events["basic_return"].add(on_return)
    channel.confirm_select()
    try:
        while len(moved) < limit:
            if wanted is not None and not wanted:
                break
            if scanned >= REQUEUE_SCAN_MAX:
                stopped = f"looked through {REQUEUE_SCAN_MAX} messages"
                break
            if clock() - started > budget_seconds:
                stopped = f"took longer than {budget_seconds:g}s"
                break
            message = channel.basic_get(queue=name, no_ack=False)
            if message is None:
                break
            scanned += 1
            tag = message.delivery_info["delivery_tag"]
            properties = dict(message.properties or {})
            headers = dict(properties.get("application_headers") or {})
            key = message_key(properties, headers)
            if wanted is not None and key not in wanted:
                held.append(tag)
                continue
            if wanted is not None:
                wanted.discard(key)
            destination = _destination(headers)
            if destination is None:
                refused.append({"key": key, "reason": "no x-death header, so where it came from is unknown"})
                held.append(tag)
                continue
            deaths = death_count(headers)
            if deaths > max_deaths and not force:
                refused.append({"key": key, "reason": f"dead-lettered {deaths} times (limit {max_deaths}); "
                                                      "requeue it with force to send it anyway"})
                held.append(tag)
                continue
            headers[REQUEUE_HEADER] = int(headers.get(REQUEUE_HEADER) or 0) + 1
            properties["application_headers"] = headers
            exchange, routing_key = destination
            try:
                copy = AmqpMessage(message.body, **properties)
                returned.clear()
                channel.basic_publish_confirm(copy, exchange=exchange, routing_key=routing_key,
                                              mandatory=True, confirm_timeout=confirm_timeout)
                if returned:
                    raise _Returned(f"the broker could not route it to exchange {exchange!r} with key "
                                    f"{routing_key!r} ({returned[0]}); is the original queue still bound?")
            except Exception as exc:  # noqa: BLE001 — nack, unroutable, timeout, dead connection: all stop the run
                error = str(exc) if isinstance(exc, _Returned) else f"publishing {key or 'a message'} failed: {exc!r}"
                held.append(tag)
                break
            # Confirmed and routed: only now is the original safe to drop.
            channel.basic_ack(tag)
            moved.append({"key": key, "exchange": exchange, "routing_key": routing_key})
    except Exception as exc:  # noqa: BLE001 — basic_get/ack on a broken channel
        error = error or f"the broker connection failed: {exc!r}"
    finally:
        for tag in held:
            try:
                channel.basic_reject(tag, requeue=True)
            except Exception:  # noqa: BLE001, S110 — closing the channel hands them back anyway
                pass
        channel.events["basic_return"].discard(on_return)
    not_found = sorted(wanted) if wanted else []
    return {
        "queue": name,
        "moved": len(moved),
        "moved_messages": moved,
        "refused": refused,
        "not_found": not_found,
        "error": error,
        "stopped": stopped,
    }


def _open_amqp(celery_app: Celery, timeout: float):
    opts = dict(celery_app.conf.broker_transport_options or {})
    # Bounds every socket read/write (basic.get, confirms) as well as the
    # connect, so a broker that stops answering ends the run with an error.
    opts.update(read_timeout=timeout, write_timeout=timeout)
    conn = celery_app.connection_for_write(connect_timeout=CONNECT_TIMEOUT, transport_options=opts)
    conn.connect()
    return conn


def requeue_dlq(config, session, name: str, username: str, *, keys: list[str] | None = None,
                limit: int = REQUEUE_DEFAULT, force: bool = False, open_connection=None) -> dict:
    """Requeue messages from DLQ `name` (see `requeue_on_channel`); audited.

    Checks first, over the management API, that `name` really is a
    dead-letter queue. Raises RabbitMQNotConfigured, RabbitMQNotFound or
    RabbitMQError before anything is taken; once the AMQP side runs, every
    outcome (including a mid-way failure) is returned and audited.
    """
    reason = not_configured_reason(config)
    if reason:
        raise RabbitMQNotConfigured(reason)
    limit = max(1, min(int(limit), REQUEUE_MAX))
    _with_deadline(config, lambda client: _require_dlq(config, client, name))
    try:
        conn = (open_connection or _open_amqp)(config.celery_app, float(config.rabbitmq_management_timeout_seconds) + 2.0)
    except Exception as exc:  # noqa: BLE001 — refused, auth failure: nothing taken yet
        raise RabbitMQError(f"could not connect to the broker: {exc}") from exc
    try:
        result = requeue_on_channel(
            conn.channel(), name, keys=set(keys) if keys is not None else None,
            limit=limit if keys is None else min(len(keys), REQUEUE_MAX), force=force,
        )
    finally:
        try:
            conn.release()
        except Exception:  # noqa: BLE001, S110 — best-effort release
            pass
    record_audit(session, username, "requeue_dead_letters", "rabbitmq_queue", name, {
        "moved": result["moved"],
        "task_ids": [m["key"] for m in result["moved_messages"]],
        "refused": len(result["refused"]),
        "forced": force,
        "error": result["error"],
    })
    return result
