"""Create (or fail on duplicate) a dashboard user.

    python create_user.py admin 'a-strong-password'
"""
import sys

import settings
from celery_app import app

from celery_manager.auth.users import create_user
from celery_manager.db.session import get_session_factory, init_db

if len(sys.argv) != 3:
    raise SystemExit("usage: python create_user.py USERNAME PASSWORD")

session_factory = get_session_factory(init_db(settings.make_config(app)))
with session_factory() as session:
    create_user(session, sys.argv[1], sys.argv[2])
print(f"created dashboard user {sys.argv[1]!r}")
