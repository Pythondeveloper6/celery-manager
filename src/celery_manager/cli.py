from __future__ import annotations

import argparse
import importlib

from celery_manager.config import CeleryManagerConfig
from celery_manager.db.session import init_db, get_session_factory
from celery_manager.events.listener import EventListener


def main() -> None:
    parser = argparse.ArgumentParser(prog="celery-manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    events_parser = subparsers.add_parser("events", help="run the events listener")
    events_parser.add_argument("--celery-app", required=True, help="import path, e.g. demo.celery:app")
    events_parser.add_argument("--db-url", required=True)
    events_parser.add_argument("--secret-key", default="change-me")

    args = parser.parse_args()

    if args.command == "events":
        module_path, attr = args.celery_app.split(":")
        module = importlib.import_module(module_path)
        celery_app = getattr(module, attr)

        config = CeleryManagerConfig(celery_app=celery_app, db_url=args.db_url, secret_key=args.secret_key)
        engine = init_db(config)
        session_factory = get_session_factory(engine)
        EventListener(config, session_factory).run()


if __name__ == "__main__":
    main()
