from __future__ import annotations

import argparse
import importlib
import os
import sys

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

    args = parser.parse_args()

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
