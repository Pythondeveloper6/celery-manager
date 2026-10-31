"""Read-only deployment preflight. Never initializes or repairs a database."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from threading import Event, Lock, Thread

from sqlalchemy import text

_registry_lock = Lock()


def _bounded(config, name, fn, timeout):
    # At most one outstanding read per check/config. Repeated preflight
    # requests must not multiply a driver call that ignores timeouts.
    with _registry_lock:
        pending = getattr(config, "_cm_doctor_pending", None)
        if pending is None:
            pending = config._cm_doctor_pending = {}
        current = pending.get(name)
        if current is None or current[0].is_set():
            done, result = Event(), []
            def invoke():
                try:
                    result.append((True, fn()))
                except Exception as exc:  # noqa: BLE001 - diagnostics expose error classes only
                    result.append((False, type(exc).__name__))
                finally:
                    done.set()
            current = pending[name] = (done, result)
            Thread(target=invoke, daemon=True, name="cm-doctor-" + name).start()
    if not current[0].wait(timeout):
        return False, "timeout"
    return current[1][0] if current[1] else (False, "check_aborted")


def doctor(config, *, check_broker=False, check_workers=False, timeout=5):
    """Return bounded, redacted check results; outbound checks require opt-in."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
        raise ValueError("timeout must be in (0, 60] seconds")
    checks = []
    def add(name, state, detail):
        checks.append({"name": name, "state": state, "detail": detail})
    from celery_manager.db.migrate import migration_status
    from celery_manager.db.session import create_db_engine
    def storage():
        record = getattr(config, "_cm_runtime_record", None)
        from sqlalchemy.engine import make_url
        if record is not None and (record[1].url != make_url(config.db_url) or record[1].get_execution_options().get("schema_translate_map", {}).get(None) != config.db_schema):
            record = None
        storage_url = config.db_url
        engine_options = dict(config.engine_options or {})
        if record is None:
            from pathlib import Path
            from urllib.parse import quote

            url = make_url(config.db_url)
            if url.drivername.startswith("sqlite") and url.database and url.database != ":memory:":
                uri = str(url.query.get("uri", "")).lower() in ("1", "true", "yes") or bool(
                    (config.engine_options or {}).get("connect_args", {}).get("uri"))
                memory_uri = uri and url.database.startswith("file:") and (
                    url.database == "file::memory:" or url.query.get("mode") == "memory")
                if not memory_uri:
                    # SQLite's default rwc mode creates missing files on connect.
                    # A read-only URI also closes the exists/connect race and
                    # covers configured file: URIs without provisioning storage.
                    database = url.database if uri else "file:" + quote(str(Path(url.database).resolve()), safe="/")
                    storage_url = url.set(database=database, query={**url.query, "mode": "ro", "uri": "true"}).render_as_string(hide_password=False)
                    engine_options["connect_args"] = {**engine_options.get("connect_args", {}), "uri": True}
        engine = record[1] if record else create_db_engine(storage_url, db_schema=config.db_schema, engine_options=engine_options)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            state = migration_status(engine)
            return state.up_to_date
        finally:
            if record is None:
                engine.dispose()
    ok, current = _bounded(config, "storage", storage, timeout)
    add("storage_and_migrations", "pass" if ok and current else "fail",
        "Storage reachable and migrations current" if ok and current else
        "Storage reachable; run the explicit migrate command" if ok else "Storage check failed: " + current)
    external = config.authenticate_request is not None or bool(config.principal_providers)
    safe_key = bool(config.secret_key and config.secret_key.strip() and config.secret_key != "change-me")
    add("authentication", "pass" if safe_key and (external or config.local_login_enabled) else "fail",
        "Set a private dashboard secret_key" if not safe_key else
        "Configure a principal provider or enable local login" if not (external or config.local_login_enabled) else
        "Authentication configuration present; verify actual permissions in the host integration")
    add("tenant_isolation", "pass" if config.tenant_resolver else "info",
        "Trusted tenant resolver enabled; task-history-only access" if config.tenant_resolver else
        "Global operator deployment; do not expose this dashboard to customer tenants")
    add("storage_isolation", "pass" if config.db_schema or config.db_url.startswith("sqlite") else "info",
        "Dedicated storage/schema must be provisioned separately from the host application database")
    add("operator_safeguards", "pass" if config.operator_safeguards else "info",
        "Action reasons and environment confirmation enabled" if config.operator_safeguards else "Operator guardrails are not enabled")
    observers = list(getattr(config, "_cm_observers", ()))
    failed = any(observer.health()["writes_failed"] for observer in observers)
    add("observer_telemetry", "fail" if failed else "pass" if observers else "info",
        "An observer reported telemetry write failures" if failed else "Observation instrumentation configured" if observers else "No native observers registered in this process")
    app = config.celery_app
    if app is None:
        add("execution_backend", "pass", "Observation-only mode; host executor behavior is unchanged")
    else:
        def registration():
            app.loader.import_default_modules()
            return len([name for name in app.tasks if not name.startswith("celery.")])
        ok, value = _bounded(config, "registration", registration, timeout)
        add("task_registration", "pass" if ok and value else "fail", f"Registered user task count: {value}" if ok else "Task import failed: " + value)
        from celery_manager.celery_checklist import review
        try:
            checks.extend(finding.as_check() for finding in review(app))
        except Exception as exc:  # noqa: BLE001 - a host's custom conf object must not break preflight
            add("celery_configuration", "unknown", "Celery configuration review failed: " + type(exc).__name__)
        scheduler = str(app.conf.get("beat_scheduler") or "")
        add("scheduler_stamping", "pass" if scheduler.startswith("celery_manager.beat") else "info",
            "Configured stamping scheduler" if scheduler.startswith("celery_manager.beat") else "Verify the actual beat launch command uses the stamping scheduler")
        if check_broker:
            def broker():
                with app.connection_for_read(connect_timeout=timeout) as connection:
                    connection.ensure_connection(max_retries=0)
                return True
            ok, value = _bounded(config, "broker", broker, timeout)
            add("broker", "pass" if ok else "fail", "Broker connection succeeded" if ok else "Broker check failed: " + value)
        if check_workers:
            ok, value = _bounded(config, "workers", lambda: app.control.inspect(timeout=timeout).active_queues(), timeout + 0.5)
            queues = {q.get("name") for entries in value.values() for q in entries if isinstance(q, dict)} if ok and isinstance(value, dict) else set()
            add("worker_consumers", "pass" if queues else "unknown", f"Observed queue count: {len(queues)}; inspect timeout is not proof that workers are absent")
        if not check_broker or not check_workers:
            add("outbound_checks", "info", "Use --check-broker and --check-workers to request broker/worker probes")
    return {"ok": not any(c["state"] == "fail" for c in checks), "environment": config.environment_name,
            "checked_at": datetime.now(timezone.utc).isoformat(), "checks": checks}


def render_report(report):
    return json.dumps(report, indent=2, sort_keys=True)
