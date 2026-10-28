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


def migrate(db_url: str, *, check: bool = False) -> int:
    """`celery-manager migrate`. Returns the process exit code."""
    engine = create_db_engine(db_url)
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


def _user_session_factory(db_url: str):
    # Same path every other process takes (init_db honours auto_migrate), so
    # the users table, and its role column, exist before we write to it.
    config = CeleryManagerConfig(celery_app=None, db_url=db_url)
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


def create_user_command(db_url: str, username: str, role: str, *, password_stdin: bool = False) -> int:
    """`celery-manager create-user`. Returns the process exit code."""
    from celery_manager.auth.users import create_user, user_exists

    engine, session_factory = _user_session_factory(db_url)
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


def set_role_command(db_url: str, username: str, role: str) -> int:
    """`celery-manager set-role`. Returns the process exit code."""
    from celery_manager.auth.users import set_user_role

    engine, session_factory = _user_session_factory(db_url)
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


def list_users_command(db_url: str) -> int:
    """`celery-manager list-users`. Returns the process exit code."""
    from celery_manager.db.models import User

    engine, session_factory = _user_session_factory(db_url)
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


def prune_command(db_url: str, *, dry_run: bool = False, batch_size: int = 1000) -> int:
    """`celery-manager prune`. Returns the process exit code."""
    from celery_manager.control.retention import run_prune

    engine, session_factory = _user_session_factory(db_url)
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="celery-manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    events_parser = subparsers.add_parser("events", help="run the events listener")
    _add_config_args(events_parser, required=False)
    events_parser.add_argument(
        "--with-alerts",
        action="store_true",
        help="also evaluate alert rules in this process, on a background thread",
    )
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
    migrate_parser.add_argument(
        "--check",
        action="store_true",
        help="only report the current and latest revision; exit 1 if an upgrade is needed",
    )

    create_user_parser = subparsers.add_parser("create-user", help="create a dashboard user")
    create_user_parser.add_argument("username")
    create_user_parser.add_argument("--db-url", required=True)
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

    list_users_parser = subparsers.add_parser("list-users", help="list dashboard users and their roles")
    list_users_parser.add_argument("--db-url", required=True)

    prune_parser = subparsers.add_parser(
        "prune", help="delete runs older than the retention rules (a no-op until rules are set)"
    )
    prune_parser.add_argument("--db-url", required=True)
    prune_parser.add_argument("--dry-run", action="store_true", help="only report what would be deleted")
    prune_parser.add_argument("--batch-size", type=int, default=1000, help="rows per transaction (default 1000)")

    args = parser.parse_args()

    if args.command == "prune":
        sys.exit(prune_command(args.db_url, dry_run=args.dry_run, batch_size=args.batch_size))

    if args.command == "create-user":
        sys.exit(create_user_command(args.db_url, args.username, args.role, password_stdin=args.password_stdin))

    if args.command == "set-role":
        sys.exit(set_role_command(args.db_url, args.username, args.role))

    if args.command == "list-users":
        sys.exit(list_users_command(args.db_url))

    if args.command == "migrate":
        sys.exit(migrate(args.db_url, check=args.check))

    if args.command == "alerts":
        config = _load_config(args.config, args.celery_app, args.db_url)
        sys.exit(alerts_command(config, args.interval, args.once))

    if args.command == "events":
        config = _load_config(args.config, args.celery_app, args.db_url)
        engine = init_db(config)
        session_factory = get_session_factory(engine)
        if args.with_alerts:
            # Same process, own thread and own sessions: the listener's
            # capture loop blocks, and a slow webhook must not delay events.
            from celery_manager.alerts import start_in_background

            start_in_background(session_factory, config.celery_app, config, args.alerts_interval)
        # Queue depth history: its own thread and sessions, for the same
        # reason. A no-op when queue_sample_interval_seconds is 0.
        from celery_manager.control.depth_history import start_in_background as start_sampler

        start_sampler(session_factory, config.celery_app, config)
        EventListener(config, session_factory).run()


if __name__ == "__main__":
    main()
