"""The locked backoff read must not return stale identity-map attributes.

`admit_login` reads the backoff rows without a lock first (to reject an
already-blocked caller cheaply). On the generic/SQL Server path the later
locked read used to hand back that cached object unchanged, so concurrent
reservations overwrote each other's increments. SQLite takes the
upsert path, so the generic path is forced here by reporting another dialect.
"""
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select, update

from celery_manager.auth.state import _locked_backoff, now
from celery_manager.config import CeleryManagerConfig
from celery_manager.db.auth_state import LoginBackoff
from celery_manager.db.session import get_session_factory, init_db


def test_locked_read_refreshes_a_row_already_in_the_session(tmp_path):
    config = CeleryManagerConfig(celery_app=None, db_url=f"sqlite:///{tmp_path / 'cm.db'}")
    engine = init_db(config)
    sessions = get_session_factory(engine)
    try:
        with sessions() as seed:
            seed.add(LoginBackoff(key="k", attempts=1, updated_at=now()))
            seed.commit()
        with sessions() as session:
            cached = session.scalar(select(LoginBackoff).where(LoginBackoff.key == "k"))
            assert cached.attempts == 1
            with sessions() as peer:
                peer.execute(update(LoginBackoff).where(LoginBackoff.key == "k").values(attempts=7))
                peer.commit()
            real_get_bind = session.get_bind
            calls = []

            def get_bind(*args, **kwargs):
                # Only _locked_backoff's dialect check sees another dialect;
                # statement execution keeps the real SQLite engine.
                calls.append(1)
                if len(calls) == 1:
                    return SimpleNamespace(dialect=SimpleNamespace(name="mssql"))
                return real_get_bind(*args, **kwargs)

            with patch.object(session, "get_bind", side_effect=get_bind):
                row = _locked_backoff(session, "k")
            assert row is cached and row.attempts == 7
    finally:
        engine.dispose()
