from __future__ import annotations

import bcrypt

from celery_manager.db.models import User


def create_user(session, username: str, password: str) -> User:
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    user = User(username=username, password_hash=password_hash)
    session.add(user)
    session.commit()
    return user


def verify_password(session, username: str, password: str) -> User | None:
    user = session.query(User).filter_by(username=username).one_or_none()
    if user is None:
        return None
    if bcrypt.checkpw(password.encode("utf-8"), user.password_hash.encode("utf-8")):
        return user
    return None
