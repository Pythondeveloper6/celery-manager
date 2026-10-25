from __future__ import annotations

import argparse
import importlib
import os
import sys

from celery_manager.config import CeleryManagerConfig
from celery_manager.db.session import get_session_factory, init_db
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="celery-manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    events_parser = subparsers.add_parser("events", help="run the events listener")
    events_parser.add_argument("--celery-app", required=True, help="import path, e.g. demo.celery:app")
    events_parser.add_argument("--db-url", required=True)

    args = parser.parse_args()

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
