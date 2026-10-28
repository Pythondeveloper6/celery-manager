from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from celery import Celery

if TYPE_CHECKING:
    from starlette.requests import Request

    from celery_manager.auth.external import ExternalUser


@dataclass
class CeleryManagerConfig:
    """Runtime configuration for celery-manager.

    celery_app: the host application's Celery instance. Used for task
        introspection (registered task names) and control calls
        (revoke/terminate).
    db_url: SQLAlchemy connection URL for celery-manager's own storage.
        Defaults to a local SQLite file; swap to any SQLAlchemy-supported
        DB (Postgres, MySQL, ...) for production use.
    secret_key: used to sign session cookies for the built-in auth. The
        default is a public placeholder: `create_app()` refuses to start
        with it (or an empty key), since anyone could forge a session from
        it. Processes that don't serve HTTP (workers, the events listener)
        never sign sessions and may leave it as-is.
    eta_tolerance_seconds: how far apart two timestamps of the same
        interval-schedule occurrence may be and still match. With beat
        running the stamping scheduler (`celery_manager.beat:Scheduler`) the
        decorator matches a skip/cancel by the occurrence beat stamped on the
        message: crontab/solar exactly, interval schedules within this
        window (capped at half the interval). The dashboard also uses it to
        match saved overrides to listed occurrences. Only for messages with
        no occurrence headers from a beat that has never stamped one (an
        older beat during an upgrade) is it still compared with "now", the
        previous behaviour.
    missed_run_grace_seconds: how long after a scheduled run was due it may
        still arrive before the dashboard reports it as missed. Detection
        needs beat to run the stamping scheduler
        (`celery_manager.beat:Scheduler`); see the decorator docs. Raise it
        if your broker or workers are routinely slow to start a run.
    export_max_rows: hard cap on rows in one `GET /api/tasks/export` download.
        Larger results are cut off (newest first) and flagged with an
        `X-Export-Truncated: true` header.
    cookie_secure: sets the `Secure` flag on the session cookie, so the
        browser only ever sends it over HTTPS. Defaults to True — the
        session cookie is a bearer credential for the whole dashboard, and
        a default of False would silently ship every deployment that never
        thought about it with a cookie leakable over plain HTTP. Set it to
        False only for local HTTP development, where there is no HTTPS to
        send it over.
    auto_migrate: when True (the default), `init_db()` upgrades the
        database schema to the latest revision at startup, in every process
        that calls it (API, workers, events listener; concurrent upgrades
        are serialised by a lock). Set it to False to run migrations as a
        separate deploy step with `celery-manager migrate`; `init_db()`
        then leaves the database untouched and only logs a warning if the
        schema isn't current.
    inspect_timeout_seconds: how long the Workers page (and the worker
        control endpoints) wait for workers to answer a broadcast
        (`inspect()`, `pool_grow`, `rate_limit`, ...). Kept short because
        the request blocks for it: a worker that hasn't answered within a
        second is, for an operator, effectively offline.
    live_poll_interval_seconds: how often, while at least one dashboard
        has the live stream (`/api/stream`) open, each API process polls
        `task_runs` for rows other processes changed (the events listener,
        workers) and pushes them to the browser. Lower is snappier and
        costs one small indexed query per interval per API process; nothing
        is polled while nobody is watching.
    schedule_editing: allow operators to change a schedule's crontab or
        interval from the dashboard. Off by default because an edit only
        takes effect when beat runs celery-manager's database-backed
        scheduler (`celery -A app beat -S celery_manager.beat:DatabaseScheduler`,
        with `app.conf.celery_manager_db_url` pointing at this `db_url`);
        with the plain `Scheduler`, or any other, beat never reads the edit
        and the dashboard would promise runs that don't happen. While False
        the edit route answers 409, saved edits are ignored when listing
        upcoming runs, and the dashboard shows the editor disabled with the
        reason. Turn it on in the API process once beat runs that scheduler.
    metrics_token: bearer token for `GET /metrics` (Prometheus). None, the
        default, disables the endpoint entirely: it answers 404. When set,
        scrapers must send `Authorization: Bearer <token>`. Treat it like a
        password; unlike the dashboard session it is not tied to a user.
    metrics_cache_seconds: how long the computed metrics snapshot is reused,
        so many scrapers (or a scrape storm) cost one database pass and one
        broker round trip per interval. 0 disables the cache.
    dashboard_url: the dashboard's public address, e.g.
        `https://ops.example.com/celery/`. Alert messages link to the
        relevant page under it (the task, queue, worker or schedule). None,
        the default, sends messages without links: a process outside the
        web server cannot work out the URL people reach the dashboard on.
    smtp_host: SMTP server for email alert channels. None (the default)
        disables email delivery: an email channel's messages then fail
        with an explanation, recorded on the channel. SMTP settings live
        here, never in the database, so the password is never part of an
        API response or a database dump.
    smtp_port: SMTP port. 587 (submission with STARTTLS) by default.
    smtp_username: SMTP login. None sends without authenticating.
    smtp_password: SMTP password, used only with `smtp_username`.
    smtp_use_tls: upgrade the SMTP connection with STARTTLS before
        logging in. True by default; turn it off only for a local relay.
    alert_email_from: the From address of alert emails. Required for
        email channels; there is no safe default.
    alert_send_timeout_seconds: the time limit for each outbound alert
        delivery (one Slack post, one webhook call, one SMTP session). A
        channel that doesn't answer in time is recorded as failed and the
        other channels are still tried.
    alert_interval_seconds: how often the alerts evaluator
        (`celery-manager alerts`, or `events --with-alerts`) checks every
        rule. Each check costs one worker ping (only with worker_offline
        rules), one broker queue listing (only with queue_backlog rules)
        and a few indexed `task_runs` queries.
    queue_sample_interval_seconds: how often the events listener
        (`celery-manager events`) records every queue's broker depth for the
        depth history charts and the `queue_growing` alert. 0 turns sampling
        off. Each sample is one queue listing (the same deadline-bounded
        broker call the Queues page makes) and one small insert per queue.
        With several listener processes only one samples per interval
        (best-effort; see docs/queues.md).
    queue_sample_retention_days: how long depth samples are kept. Older ones
        are deleted by the retention prune and, hourly, by the sampler
        itself. 0 keeps them forever.
    authenticate_request: sign in with the host application's own users
        instead of (or as well as) celery-manager's accounts. A callable
        taking the Starlette ``Request`` and returning
        ``celery_manager.integrations.ExternalUser(username, role)``, or None
        when the request isn't signed in to the host. It is asked first, on
        every request, so the host's answer (including a demotion or a
        removed user) applies immediately; None falls back to the local
        ``cm_session`` accounts. ``role=None`` means "a host user with no
        access" and is a 403; an unknown role, a wrong return type or an
        exception is a 500 (fail closed, never a fallback). Called from a
        worker thread, so it may block on an ORM query; an ``async def``
        works too. Ready-made ones for Django and Flask live in
        ``celery_manager.integrations``; see docs/integrations.md.
    local_login_enabled: whether celery-manager's own accounts can still
        sign in. Set it to False with ``authenticate_request`` to make the
        host app the only way in: the login form is replaced by a "Sign in
        with ..." link, ``POST /api/auth/login`` is refused and existing
        ``cm_session`` cookies stop working. ``create_app()`` refuses False
        without an authenticator, since nobody could sign in.
    login_url: where the dashboard sends people to sign in to the host app
        (a path like ``/accounts/login/?next={next}`` or an http(s) URL).
        ``{next}`` is replaced with the dashboard's own address, URL-encoded,
        so the host can send them back. Without it the dashboard can only
        say "sign in to <host> first".
    logout_url: where "Sign out" goes for someone signed in through the host
        (a plain link, so a GET). Without it, those users get no sign-out
        button in the dashboard: clearing celery-manager's cookie would not
        sign them out of the host, and they would be straight back in.
    login_provider_name: what the dashboard calls the host app on the sign-in
        screen and in the sidebar ("Sign in with Acme Admin").
    queue_browser_show_args: show task arguments when browsing the messages
        waiting in a queue (`GET /api/queues/{name}/messages`). True by
        default, matching task runs, where every role sees arguments (see
        docs/roles.md). Set it to False if your task arguments carry secrets
        you don't want read off the broker: the arguments are then left out
        of the response for every role, not just hidden in the dashboard.
    queue_browser_amqp: allow browsing RabbitMQ queues. Off by default
        because RabbitMQ can't show a message without handing it out: each
        message looked at is taken without acknowledgement and rejected back
        onto the queue, which marks it redelivered and may move it to the
        front. While False the endpoint answers `supported: false` with the
        reason. Redis browsing is read-only and needs no switch.
    """

    celery_app: Celery
    db_url: str = "sqlite:///celery_manager.db"
    secret_key: str = "change-me"
    eta_tolerance_seconds: int = 60
    missed_run_grace_seconds: int = 300
    cookie_secure: bool = True
    export_max_rows: int = 50_000
    auto_migrate: bool = True
    inspect_timeout_seconds: float = 1.0
    live_poll_interval_seconds: float = 1.0
    schedule_editing: bool = False
    metrics_token: str | None = None
    metrics_cache_seconds: float = 5.0
    dashboard_url: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    # Kept out of repr() so a logged or printed config doesn't carry it.
    smtp_password: str | None = field(default=None, repr=False)
    smtp_use_tls: bool = True
    alert_email_from: str | None = None
    alert_send_timeout_seconds: float = 10.0
    alert_interval_seconds: float = 60.0
    queue_sample_interval_seconds: float = 60.0
    queue_sample_retention_days: int = 14
    authenticate_request: Callable[[Request], ExternalUser | None | Awaitable[ExternalUser | None]] | None = None
    local_login_enabled: bool = True
    login_url: str | None = None
    logout_url: str | None = None
    login_provider_name: str = "your app"
    queue_browser_show_args: bool = True
    queue_browser_amqp: bool = False
