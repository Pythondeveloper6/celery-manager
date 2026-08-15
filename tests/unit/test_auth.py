# tests/unit/test_auth.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.db.models import Base
from celery_manager.auth.users import create_user, verify_password
from celery_manager.auth.sessions import sign_session, verify_session


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_create_and_verify_password():
    session = _session()
    create_user(session, "alice", "correct-horse-battery-staple")

    assert verify_password(session, "alice", "correct-horse-battery-staple") is not None
    assert verify_password(session, "alice", "wrong-password") is None
    assert verify_password(session, "bob", "anything") is None


def test_password_is_hashed_not_stored_plain():
    session = _session()
    user = create_user(session, "alice", "correct-horse-battery-staple")
    assert user.password_hash != "correct-horse-battery-staple"


def test_session_round_trip():
    token = sign_session("test-secret", "alice")
    assert verify_session("test-secret", token) == "alice"


def test_session_rejects_wrong_secret():
    token = sign_session("test-secret", "alice")
    assert verify_session("different-secret", token) is None
