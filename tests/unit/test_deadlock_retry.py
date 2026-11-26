"""A database-chosen deadlock victim restarts its transaction instead of losing the write."""
import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from celery_manager.db import runs as runs_module
from celery_manager.db.runs import commit_with_deadlock_retry, is_deadlock


class _MySQLDeadlock(Exception):
    args = (1213, "Deadlock found when trying to get lock; try restarting transaction")


class _PostgresDeadlock(Exception):
    pgcode = "40P01"


def _error(orig, cls=OperationalError):
    return cls("UPDATE task_runs ...", {}, orig)


class _Session:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(runs_module.time, "sleep", lambda _seconds: None)


@pytest.mark.parametrize("orig", [
    _MySQLDeadlock(),
    _PostgresDeadlock(),
    Exception("Transaction (Process ID 55) was deadlocked on lock resources and has been chosen as the deadlock victim"),
])
def test_deadlock_victims_are_recognised(orig):
    assert is_deadlock(_error(orig))


@pytest.mark.parametrize("exc", [
    _error(Exception("duplicate key"), IntegrityError),
    _error(Exception("Lock wait timeout exceeded; try restarting transaction")),
    ValueError("deadlock"),  # not a database error at all
])
def test_other_errors_are_not_deadlocks(exc):
    assert not is_deadlock(exc)


def test_restarts_the_whole_transaction_then_commits_once():
    session, calls = _Session(), []

    def work():
        calls.append(1)
        if len(calls) < 3:
            raise _error(_MySQLDeadlock())
        return "done"

    assert commit_with_deadlock_retry(session, work) == "done"
    assert len(calls) == 3 and session.rollbacks == 2 and session.commits == 1


def test_a_deadlock_that_never_clears_is_raised_after_the_attempt_limit():
    session, calls = _Session(), []

    def work():
        calls.append(1)
        raise _error(_MySQLDeadlock())

    with pytest.raises(DBAPIError):
        commit_with_deadlock_retry(session, work, attempts=3)
    assert len(calls) == 3 and session.commits == 0


def test_deadlock_on_commit_is_also_retried():
    class FlakyCommit(_Session):
        def commit(self):
            super().commit()
            if self.commits == 1:
                raise _error(_MySQLDeadlock())

    session, calls = FlakyCommit(), []
    commit_with_deadlock_retry(session, lambda: calls.append(1))
    assert len(calls) == 2 and session.commits == 2 and session.rollbacks == 1


def test_other_errors_propagate_untouched_on_the_first_try():
    session, calls = _Session(), []

    def work():
        calls.append(1)
        raise _error(Exception("duplicate key"), IntegrityError)

    with pytest.raises(IntegrityError):
        commit_with_deadlock_retry(session, work)
    assert len(calls) == 1 and session.rollbacks == 0
