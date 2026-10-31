from __future__ import annotations

import argparse
import getpass
import importlib
import os
import sys

from celery_manager.auth.roles import ROLES, VIEWER
from celery_manager.config import CeleryManagerConfig
from celery_manager.db.migrate import migration_status, upgrade_database
from celery_manager.db.session import create_db_engine, get_session_factory, init_db
from celery_manager.events.listener import EventListener


def load_celery_app(path: str):
    """Import "module:attr". Console-script entry points, unlike `celery -A` and
    uvicorn, do not put the current directory on sys.path, so the user's own
    module (run from their project dir) would not be importable; mirror celery."""
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    module_path, attr = path.split(":")
    return getattr(importlib.import_module(module_path), attr)


def migrate(db_url: str, *, check: bool = False, db_schema=None) -> int:
    """`celery-manager migrate`. Returns the process exit code."""
    engine = create_db_engine(db_url, db_schema=db_schema)
    try:
        status = migration_status(engine)
        if status.current is not None:
            current = status.current
        else:
            current = "unversioned (created before migrations)" if status.legacy else "empty"
        print(f"current revision: {current}")
        print(f"latest revision:  {status.head}")
        if check:
            if status.up_to_date:
                print("Schema is up to date.")
                return 0
            print("Schema needs an upgrade: run `celery-manager migrate --db-url ...`.")
            return 1
        if status.up_to_date:
            print("Schema is up to date; nothing to do.")
            return 0
        upgrade_database(engine)
        print(f"Upgraded to {migration_status(engine).current}.")
        return 0
    finally:
        engine.dispose()


def _user_session_factory(db_url: str, db_schema=None):
    # Same path every other process takes (init_db honours auto_migrate), so
    # the users table, and its role column, exist before we write to it.
    config = CeleryManagerConfig(celery_app=None, db_url=db_url, db_schema=db_schema)
    engine = init_db(config)
    return engine, get_session_factory(engine)


def _read_password(from_stdin: bool) -> str:
    # Never a --password flag: argv is visible to every local user in `ps`
    # and lands in shell history.
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    password = getpass.getpass("Password: ")
    if getpass.getpass("Repeat password: ") != password:
        raise ValueError("passwords do not match")
    return password


def create_user_command(db_url: str, username: str, role: str, *, password_stdin: bool = False, db_schema=None) -> int:
    """`celery-manager create-user`. Returns the process exit code."""
    from celery_manager.auth.users import create_user, user_exists

    engine, session_factory = _user_session_factory(db_url, db_schema=db_schema)
    try:
        with session_factory() as session:
            if user_exists(session, username):
                print(f"error: a dashboard user named {username!r} already exists", file=sys.stderr)
                return 1
            try:
                create_user(session, username, _read_password(password_stdin), role=role)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
        print(f"Created dashboard user {username!r} with role {role!r}.")
        return 0
    finally:
        engine.dispose()


def set_role_command(db_url: str, username: str, role: str, *, db_schema=None) -> int:
    """`celery-manager set-role`. Returns the process exit code."""
    from celery_manager.auth.users import set_user_role

    engine, session_factory = _user_session_factory(db_url, db_schema=db_schema)
    try:
        with session_factory() as session:
            try:
                set_user_role(session, username, role)
            except LookupError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
        print(f"{username!r} is now {role!r}; it applies from their next request.")
        return 0
    finally:
        engine.dispose()


def list_users_command(db_url: str, *, db_schema=None) -> int:
    """`celery-manager list-users`. Returns the process exit code."""
    from celery_manager.db.models import User

    engine, session_factory = _user_session_factory(db_url, db_schema=db_schema)
    try:
        with session_factory() as session:
            rows = session.query(User.username, User.role).order_by(User.username).all()
        if not rows:
            print("No dashboard users.")
        for username, role in rows:
            print(f"{username}\t{role}")
        return 0
    finally:
        engine.dispose()


def prune_command(db_url: str, *, dry_run: bool = False, batch_size: int = 1000, db_schema=None) -> int:
    """`celery-manager prune`. Returns the process exit code."""
    from celery_manager.control.retention import run_prune

    engine, session_factory = _user_session_factory(db_url, db_schema=db_schema)
    try:
        with session_factory() as session:
            try:
                summary = run_prune(session, username="cli", batch_size=batch_size, dry_run=dry_run)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
        verb = "Would delete" if dry_run else "Deleted"
        for task_name, count in sorted(summary["by_task"].items()):
            print(f"{task_name}\t{count}")
        print(
            f"{verb} {summary['runs_deleted']} run(s), {summary['audit_deleted']} audit log row(s) "
            f"and {summary['samples_deleted']} queue depth sample(s)."
        )
        if summary["runs_deleted"] == 0 and summary["audit_deleted"] == 0 and summary["samples_deleted"] == 0 and not dry_run:
            print("Nothing to prune. (Retention is off until you set days in Settings or update_retention().)")
        return 0
    finally:
        engine.dispose()


def _load_config(config_path: str | None, celery_app_path: str | None, db_url: str | None) -> CeleryManagerConfig:
    """The config for a long-running command: either an importable
    CeleryManagerConfig (`--config module:attr`, the only way to give the
    SMTP settings and dashboard_url, which have no flags of their own: a
    password on the command line is visible to every local user in `ps`),
    or one built from `--celery-app` and `--db-url`."""
    if config_path:
        config = load_celery_app(config_path)
        if not isinstance(config, CeleryManagerConfig):
            raise SystemExit(f"error: {config_path} is not a CeleryManagerConfig")
        if db_url:
            config.db_url = db_url
        if celery_app_path:
            config.celery_app = load_celery_app(celery_app_path)
        return config
    if not celery_app_path or not db_url:
        raise SystemExit("error: give --config, or both --celery-app and --db-url")
    # No secret_key: these processes never sign or verify a session, so the
    # config default is fine here (create_app() is what refuses it).
    return CeleryManagerConfig(celery_app=load_celery_app(celery_app_path), db_url=db_url)


def alerts_command(config: CeleryManagerConfig, interval: float | None, once: bool) -> int:
    """`celery-manager alerts`. Returns the process exit code (only with --once;
    otherwise it runs until interrupted)."""
    from celery_manager.alerts import evaluate_once, run_forever

    engine = init_db(config)
    session_factory = get_session_factory(engine)
    session_factory.configure(info={"cm_config": config})
    try:
        if once:
            sent = evaluate_once(session_factory, config.celery_app, config)
            for entry in sent:
                failed = f" ({len(entry['errors'])} channel(s) failed)" if entry["errors"] else ""
                print(f"{entry['event']}: rule {entry['rule_id']}, {entry['key']}{failed}")
            print(f"{len(sent)} message(s) sent.")
            return 0
        run_forever(session_factory, config.celery_app, config, interval)
        return 0
    finally:
        engine.dispose()


def _add_config_args(parser: argparse.ArgumentParser, *, required: bool) -> None:
    parser.add_argument(
        "--celery-app", "--app", dest="celery_app", required=required, help="import path, e.g. demo.celery:app"
    )
    parser.add_argument("--db-url", required=required)
    parser.add_argument(
        "--config",
        help="import path of a CeleryManagerConfig, e.g. myproject.cm:config "
        "(for SMTP and dashboard_url; --celery-app/--db-url then override it)",
    )


def build_parser() -> argparse.ArgumentParser:
    """The `celery-manager` argument parser, separate from `main` so tests and
    documentation tooling can inspect the command set without running it."""
    parser = argparse.ArgumentParser(prog="celery-manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    events_parser = subparsers.add_parser("events", help="run the events listener")
    _add_config_args(events_parser, required=False)
    events_parser.add_argument(
        "--with-alerts",
        action="store_true",
        help="also evaluate alert rules in this process, on a background thread",
    )
    events_parser.add_argument("--enable-events", action=argparse.BooleanOptionalAction, default=True)
    events_parser.add_argument("--alerts-interval", type=float, help="seconds between alert checks")

    alerts_parser = subparsers.add_parser("alerts", help="evaluate alert rules and send messages")
    _add_config_args(alerts_parser, required=False)
    alerts_parser.add_argument(
        "--interval", type=float, help="seconds between checks (default: alert_interval_seconds, 60)"
    )
    alerts_parser.add_argument("--once", action="store_true", help="check every rule once and exit")

    migrate_parser = subparsers.add_parser(
        "migrate", help="upgrade the database schema to the latest revision"
    )
    migrate_parser.add_argument("--db-url", required=True)
    migrate_parser.add_argument("--db-schema")
    migrate_parser.add_argument(
        "--check",
        action="store_true",
        help="only report the current and latest revision; exit 1 if an upgrade is needed",
    )

    create_user_parser = subparsers.add_parser("create-user", help="create a dashboard user")
    create_user_parser.add_argument("username")
    create_user_parser.add_argument("--db-url", required=True)
    create_user_parser.add_argument("--db-schema")
    create_user_parser.add_argument(
        "--role",
        choices=ROLES,
        default=VIEWER,
        help="viewer (read-only, the default), operator (day-to-day controls) or admin (everything)",
    )
    create_user_parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from the first line of stdin instead of prompting",
    )

    set_role_parser = subparsers.add_parser("set-role", help="change a dashboard user's role")
    set_role_parser.add_argument("username")
    set_role_parser.add_argument("role", choices=ROLES)
    set_role_parser.add_argument("--db-url", required=True)
    set_role_parser.add_argument("--db-schema")

    list_users_parser = subparsers.add_parser("list-users", help="list dashboard users and their roles")
    list_users_parser.add_argument("--db-url", required=True)
    list_users_parser.add_argument("--db-schema")

    prune_parser = subparsers.add_parser(
        "prune", help="delete runs older than the retention rules (a no-op until rules are set)"
    )
    prune_parser.add_argument("--db-url", required=True)
    prune_parser.add_argument("--db-schema")
    prune_parser.add_argument("--dry-run", action="store_true", help="only report what would be deleted")
    prune_parser.add_argument("--batch-size", type=int, default=1000, help="rows per transaction (default 1000)")

    import_parser = subparsers.add_parser("import-pauses", help="validate/import legacy schedule pauses from CSV")
    _add_config_args(import_parser, required=False)
    import_parser.add_argument("csv_file")
    import_parser.add_argument("--actor", required=True)
    import_parser.add_argument("--apply", action="store_true", help="apply after validation; default is dry-run")

    doctor_parser = subparsers.add_parser("doctor", help="read-only deployment preflight")
    _add_config_args(doctor_parser, required=False)
    doctor_parser.add_argument("--check-broker", action="store_true")
    doctor_parser.add_argument("--check-workers", action="store_true")
    doctor_parser.add_argument("--timeout", type=float, default=5)
    copy_parser = subparsers.add_parser(
        "copy-schedules",
        help="copy schedule definitions between scheduler backends (dry run unless --apply)",
    )
    _add_config_args(copy_parser, required=False)
    copy_parser.add_argument("--from", dest="source", required=True, choices=("code", "redbeat", "sql"),
                             help="code = the app's beat_schedule")
    copy_parser.add_argument("--to", dest="target", required=True, choices=("redbeat", "sql"))
    copy_parser.add_argument("--beat-db-url", help="SQL scheduler database (default: the app's beat_dburi)")
    copy_parser.add_argument("--beat-schema", help="SQL scheduler schema (default: the app's beat_schema)")
    copy_parser.add_argument("--allow-updates", action="store_true",
                             help="also overwrite target entries whose definition differs")
    copy_parser.add_argument("--apply", action="store_true", help="write the plan; default prints it only")
    return parser


def _schedule_backend(kind: str, app, beat_db_url: str | None, beat_schema: str | None):
    from celery_manager.schedulers import (
        RedBeatBackend,
        SQLAlchemyBeatBackend,
        StaticBeatBackend,
    )

    if kind == "code":
        return StaticBeatBackend(app)
    if kind == "redbeat":
        return RedBeatBackend(app)
    return SQLAlchemyBeatBackend(app, beat_db_url, schema=beat_schema)


def copy_schedules_command(app, source: str, target: str, *, apply: bool = False, allow_updates: bool = False,
                           beat_db_url: str | None = None, beat_schema: str | None = None) -> int:
    """`celery-manager copy-schedules`. Prints the plan (and what was applied)
    as JSON. Returns the process exit code."""
    import json

    from celery_manager.schedulers.reconcile import ReconcileConflict, copy_schedules

    if source == target:
        print("error: --from and --to must differ", file=sys.stderr)
        return 2
    backends = []
    try:
        src = _schedule_backend(source, app, beat_db_url, beat_schema)
        backends.append(src)
        dst = _schedule_backend(target, app, beat_db_url, beat_schema)
        backends.append(dst)
        result = copy_schedules(src, dst, app=app, apply=apply, allow_updates=allow_updates)
    except (ValueError, PermissionError, ReconcileConflict) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        for backend in backends:
            close = getattr(backend, "close", None)
            if close is not None:
                close()
    print(json.dumps(result, indent=2, default=str))
    pending = [c["name"] for c in result["plan"]["changes"] if c["apply"]]
    if apply:
        print(f"Applied {len(result['applied'])} change(s).", file=sys.stderr)
    else:
        print(f"Dry run: {len(pending)} change(s) would be applied; re-run with --apply.", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "doctor":
        from celery_manager.doctor import doctor, render_report
        config = _load_config(args.config, args.celery_app, args.db_url)
        try:
            result = doctor(config, check_broker=args.check_broker, check_workers=args.check_workers, timeout=args.timeout)
        except ValueError as exc:
            parser.error(str(exc))
        print(render_report(result))
        sys.exit(0 if result["ok"] else 1)

    if args.command == "import-pauses":
        import json
        from pathlib import Path

        from celery_manager.control.pause_import import MAX_BYTES, import_pauses
        from celery_manager.runtime import setup
        config = _load_config(args.config, args.celery_app, args.db_url)
        sessions = setup(config)
        path = Path(args.csv_file)
        if path.stat().st_size > MAX_BYTES:
            parser.error("CSV must be at most 1 MB")
        try:
            with sessions() as session:
                print(json.dumps(import_pauses(session, path.read_text(), args.actor, dry_run=not args.apply)))
        except ValueError as exc:
            parser.error(str(exc))
        return


    if args.command == "copy-schedules":
        if args.config:
            app = _load_config(args.config, args.celery_app, args.db_url).celery_app
        elif args.celery_app:
            app = load_celery_app(args.celery_app)
        else:
            parser.error("give --config or --celery-app")
        if app is None:
            parser.error("copy-schedules needs a Celery app")
        sys.exit(copy_schedules_command(app, args.source, args.target, apply=args.apply,
                                        allow_updates=args.allow_updates, beat_db_url=args.beat_db_url,
                                        beat_schema=args.beat_schema))

    if args.command == "prune":
        sys.exit(prune_command(args.db_url, dry_run=args.dry_run, batch_size=args.batch_size, db_schema=args.db_schema))

    if args.command == "create-user":
        sys.exit(create_user_command(args.db_url, args.username, args.role, password_stdin=args.password_stdin, db_schema=args.db_schema))

    if args.command == "set-role":
        sys.exit(set_role_command(args.db_url, args.username, args.role, db_schema=args.db_schema))

    if args.command == "list-users":
        sys.exit(list_users_command(args.db_url, db_schema=args.db_schema))

    if args.command == "migrate":
        sys.exit(migrate(args.db_url, check=args.check, db_schema=args.db_schema))

    if args.command == "alerts":
        config = _load_config(args.config, args.celery_app, args.db_url)
        sys.exit(alerts_command(config, args.interval, args.once))

    if args.command == "events":
        config = _load_config(args.config, args.celery_app, args.db_url)
        if config.celery_app is None:
            parser.error("The events listener requires Celery; native jobs use RunObserver instrumentation")
        engine = init_db(config)
        session_factory = get_session_factory(engine)
        session_factory.configure(info={"cm_config": config})
        if args.with_alerts:
            # Same process, own thread and own sessions: the listener's
            # capture loop blocks, and a slow webhook must not delay events.
            from celery_manager.alerts import start_in_background

            start_in_background(session_factory, config.celery_app, config, args.alerts_interval)
        # Queue depth history: its own thread and sessions, for the same
        # reason. A no-op when queue_sample_interval_seconds is 0.
        from celery_manager.control.depth_history import (
            start_in_background as start_sampler,
        )

        start_sampler(session_factory, config.celery_app, config)
        EventListener(config, session_factory).run(enable_events=args.enable_events)


if __name__ == "__main__":
    main()
