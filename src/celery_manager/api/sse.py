from __future__ import annotations

import asyncio
import json
import logging
import threading

from celery_manager.db.changefeed import RunChangeFeed

logger = logging.getLogger(__name__)

# Idle streams get a comment frame this often. Proxies and load balancers close
# connections that stay silent (nginx's proxy_read_timeout defaults to 60s), and
# EventSource would then reconnect in a loop. Read at call time, so tests can shrink it.
KEEPALIVE_SECONDS = 15.0


class EventBus:
    """In-process pub/sub for task-status-change events. Publishers push here;
    the /api/stream endpoint below subscribes and forwards to connected
    browsers as SSE.

    Thread-safety matters here because publishers are not necessarily on the
    event loop's thread. `asyncio.Queue` is **not** thread-safe, and calling
    `put_nowait` on one from a foreign thread does not wake the loop waiting
    on it — the item can sit in the queue unnoticed, or corrupt the queue's
    internal waiter bookkeeping. So each subscriber is stored together with
    the event loop that owns its queue (captured in `subscribe`, which always
    runs inside that loop), and `publish` hands the work back to that loop via
    `call_soon_threadsafe`, which is the supported cross-thread entry point.

    Cross-process delivery: the writes worth announcing mostly happen in
    other processes — the events listener (`celery-manager events`) and the
    workers (`@track`, `report_progress()`) — which cannot reach these
    queues. So when given a `session_factory`, the bus runs one poller
    thread that reads the `task_runs` change feed (`db/changefeed.py`) every
    `poll_interval` seconds and publishes what changed. The thread is
    started lazily by the first subscriber and stops once the last one
    leaves, so it needs no app lifespan hooks — `create_app()`'s app is
    often mounted inside a host application whose lifespan we don't own —
    and an API process with nobody watching never touches the database for
    it. Every API process polls for itself, so this also works behind a
    load balancer with several of them.
    """

    def __init__(self, session_factory=None, poll_interval: float = 1.0) -> None:
        self._subscribers: list[tuple[asyncio.Queue, asyncio.AbstractEventLoop]] = []
        self._pending: list[dict] = []  # buffer for publishes before any subscriber connects (used in tests)
        self._lock = threading.Lock()
        self._session_factory = session_factory
        self._poll_interval = poll_interval
        # The running poller's stop flag, or None when no poller is running.
        # A fresh Event per poller: a thread told to stop keeps its own set
        # flag even if a new subscriber starts a successor straight away.
        self._poller_stop: threading.Event | None = None

    def publish(self, event: dict) -> None:
        """Safe to call from any thread."""
        self._deliver(event, buffer=True)

    def _deliver(self, event: dict, buffer: bool) -> None:
        with self._lock:
            if not self._subscribers:
                # The poller passes buffer=False: a change nobody was
                # watching for is not worth replaying to a later subscriber,
                # which loads the current state when it connects anyway.
                if buffer:
                    self._pending.append(event)
                return
            subscribers = list(self._subscribers)

        for queue, loop in subscribers:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                # The loop shut down between our snapshot and this call — the
                # subscriber is gone, so dropping its copy is correct. Never
                # let a dead subscriber break delivery to the live ones.
                logger.debug("celery-manager: dropped an SSE event for a closed event loop")

    def subscribe(self) -> asyncio.Queue:
        """Must be called from inside the event loop that will consume the
        returned queue — `sse_stream` below satisfies that by construction."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            for event in self._pending:
                queue.put_nowait(event)
            self._pending.clear()
            self._subscribers.append((queue, loop))
            self._start_poller_locked()
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers = [entry for entry in self._subscribers if entry[0] is not queue]
            if not self._subscribers:
                self._stop_poller_locked()

    def close(self) -> None:
        """Stop the poller now, whether or not anyone is subscribed. Optional:
        the poller is a daemon thread that also stops with its last
        subscriber, but a host app with a shutdown hook can call this."""
        with self._lock:
            self._stop_poller_locked()

    @property
    def polling(self) -> bool:
        return self._poller_stop is not None

    def _start_poller_locked(self) -> None:
        if self._session_factory is None or self._poller_stop is not None:
            return
        stop = threading.Event()
        self._poller_stop = stop
        threading.Thread(
            target=self._poll_loop, args=(stop,), name="celery-manager-change-feed", daemon=True
        ).start()

    def _stop_poller_locked(self) -> None:
        if self._poller_stop is not None:
            self._poller_stop.set()
            self._poller_stop = None

    def _poll_loop(self, stop: threading.Event) -> None:
        # Fresh cursor per poller: a subscriber that connects fetches the
        # current state itself, so it wants changes from now on, not a replay
        # of everything since the previous poller stopped.
        feed = RunChangeFeed()
        while True:
            try:
                with self._session_factory() as session:
                    events = feed.poll(session)
            except Exception:  # deliberate: a DB hiccup must not kill live updates for good
                logger.warning("celery-manager: polling task_runs for live updates failed; retrying", exc_info=True)
                events = []
            for event in events:
                if stop.is_set():
                    return
                self._deliver(event, buffer=False)
            if stop.wait(self._poll_interval):
                return


async def sse_stream(bus: EventBus):
    queue = bus.subscribe()
    try:
        # An opening comment frame, so the response actually leaves the
        # building now. Buffering proxies (nginx, dev-server proxies) hold
        # the headers back until the first body bytes, and until they arrive
        # the browser's EventSource never fires `open` — the dashboard would
        # show the stream as down (and keep polling) for up to a keepalive
        # interval on every connect.
        yield ": connected\n\n"
        while True:
            try:
                # Cancelling queue.get() on timeout is safe: no item is consumed.
                event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield f"data: {json.dumps(event, default=str)}\n\n"
    finally:
        bus.unsubscribe(queue)
