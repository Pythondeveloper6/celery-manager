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


def main() -> None:
    parser = argparse.ArgumentParser(prog="celery-manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    events_parser = subparsers.add_parser("events", help="run the events listener")
    events_parser.add_argument("--celery-app", required=True, help="import path, e.g. demo.celery:app")
    events_parser.add_argument("--db-url", required=True)

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

    args = parser.parse_args()

    if args.command == "create-user":
        sys.exit(create_user_command(args.db_url, args.username, args.role, password_stdin=args.password_stdin))

    if args.command == "set-role":
        sys.exit(set_role_command(args.db_url, args.username, args.role))

    if args.command == "list-users":
        sys.exit(list_users_command(args.db_url))

    if args.command == "migrate":
        sys.exit(migrate(args.db_url, check=args.check))

    if args.command == "events":
        celery_app = load_celery_app(args.celery_app)

        # No secret_key: the listener never signs or verifies a session, so
        # the config default is fine here (create_app() is what refuses it).
        config = CeleryManagerConfig(celery_app=celery_app, db_url=args.db_url)
        engine = init_db(config)
        session_factory = get_session_factory(engine)
        EventListener(config, session_factory).run()


if __name__ == "__main__":
    main()
