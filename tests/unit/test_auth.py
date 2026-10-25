# tests/unit/test_auth.py
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.auth import users as users_module
from celery_manager.auth.sessions import sign_session, verify_session
from celery_manager.auth.users import create_user, verify_password
from celery_manager.db.models import Base


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


def test_session_rejects_malformed_token():
    assert verify_session("test-secret", "not-a-valid-token") is None


def test_session_rejects_valid_signature_with_corrupted_payload():
    # A token whose signature validates against the secret but whose payload is not
    # valid base64/JSON (e.g. corrupted or truncated in transit) must be rejected via
    # BadData/BadPayload, not raised uncaught, per verify_session's `str | None` contract.
    from itsdangerous import URLSafeTimedSerializer

    serializer = URLSafeTimedSerializer("test-secret", salt="celery-manager-session")
    signer = serializer.make_signer()
    corrupted_token = signer.sign("not-valid-base64-payload!!").decode("utf-8")

    assert verify_session("test-secret", corrupted_token) is None


# --- Security pass for 0.1.0a1: bcrypt's 72-byte limit and login timing ---


def test_create_user_refuses_an_empty_password():
    session = _session()
    with pytest.raises(ValueError, match="empty"):
        create_user(session, "alice", "")


def test_create_user_refuses_a_password_over_72_bytes():
    """Refused, not truncated: bcrypt only reads 72 bytes, so a truncated
    100-byte password would also accept anything sharing its first 72."""
    session = _session()
    with pytest.raises(ValueError, match="72 bytes"):
        create_user(session, "alice", "a" * 73)
    # The limit is bytes, not characters: 25 three-byte characters is 75 bytes.
    with pytest.raises(ValueError, match="72 bytes"):
        create_user(session, "alice", "€" * 25)
    assert create_user(session, "alice", "a" * 72) is not None


def test_verify_password_returns_none_for_an_over_long_password():
    session = _session()
    create_user(session, "alice", "a" * 72)
    assert verify_password(session, "alice", "a" * 100) is None
    assert verify_password(session, "bob", "a" * 100) is None


def test_missing_user_still_pays_for_a_bcrypt_check(monkeypatch):
    """Without this, a missing username returned in ~2ms and an existing one
    in ~200ms, so response time alone enumerated accounts."""
    session = _session()
    real_user = create_user(session, "alice", "correct-horse-battery-staple")

    checked_hashes = []
    real_checkpw = users_module.bcrypt.checkpw

    def spy(password, hashed):
        checked_hashes.append(hashed)
        return real_checkpw(password, hashed)

    monkeypatch.setattr(users_module.bcrypt, "checkpw", spy)

    assert verify_password(session, "nobody", "anything") is None
    assert verify_password(session, "alice", "a" * 100) is None
    assert len(checked_hashes) == 2
    # Same cost factor as a real account's hash, so the dummy check takes as
    # long as a real one: `$2b$12$...` — the cost is the second field.
    real_cost = real_user.password_hash.split("$")[2]
    assert all(h.decode().split("$")[2] == real_cost for h in checked_hashes)
