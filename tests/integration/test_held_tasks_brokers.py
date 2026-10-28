"""Delayed and stuck tasks against real brokers and real workers.

Skipped unless a broker is given:

    CM_TEST_REDIS_URL=redis://localhost:6379/0
    CM_TEST_AMQP_URL=amqp://guest:guest@localhost:5672//

Each test starts `celery worker` in a subprocess with its own queue and
node name, so nothing here depends on, or disturbs, other workers on the
same broker. The pool is one thread rather than solo: a solo worker can't
answer inspect while a task runs, and "a live worker reports it running" is
one of the things under test.
"""

from __future__ import annotations

import importlib
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from celery_manager.api.app import create_app
from celery_manager.config import CeleryManagerConfig
from celery_manager.control.unacked import list_unacked, restore_unacked
from celery_manager.control.workers import cancel_delayed_tasks, list_delayed_tasks
from celery_manager.db.models import AuditLog, TaskRun

REDIS_URL = os.environ.get("CM_TEST_REDIS_URL")
AMQP_URL = os.environ.get("CM_TEST_AMQP_URL")
ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = 1.0

needs_redis = pytest.mark.skipif(not REDIS_URL, reason="set CM_TEST_REDIS_URL to run against a real Redis")
needs_amqp = pytest.mark.skipif(not AMQP_URL, reason="set CM_TEST_AMQP_URL to run against a real RabbitMQ")


def _backend_url(broker: str) -> str | None:
    # Results go to the same Redis (another db) when there is one; the
    # AMQP tests don't need results.
    if broker.startswith("redis"):
        return broker.rsplit("/", 1)[0] + "/1"
    return None


def _celery(broker: str, keyprefix: str = ""):
    # Workers started afterwards inherit these through os.environ.
    os.environ["CM_HELD_BROKER_URL"] = broker
    if keyprefix:
        os.environ["CM_HELD_KEYPREFIX"] = keyprefix
    else:
        os.environ.pop("CM_HELD_KEYPREFIX", None)
    backend = _backend_url(broker)
    if backend:
        os.environ["CM_HELD_BACKEND_URL"] = backend
    else:
        os.environ.pop("CM_HELD_BACKEND_URL", None)
    # Fresh import so the module-level app reads this test's broker.
    # (`import_module`, not `from tests.integration import ...`: the latter
    # would hand back the old module still bound on the package.)
    sys.modules.pop("tests.integration.held_tasks_app", None)
    return importlib.import_module("tests.integration.held_tasks_app").app


def _session_factory(celery_app):
    config = CeleryManagerConfig(celery_app=celery_app, db_url="sqlite:///:memory:", secret_key="test-secret")
    _, session_factory = create_app(config)
    return session_factory


class Worker:
    def __init__(self, broker: str, queue: str):
        self.name = f"held-{uuid.uuid4().hex[:8]}@test"
        env = {
            **os.environ,
            "CM_HELD_BROKER_URL": broker,
            "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]),
        }
        backend = _backend_url(broker)
        if backend:
            env["CM_HELD_BACKEND_URL"] = backend
        self.proc = subprocess.Popen(
            [
                sys.executable, "-m", "celery", "-A", "tests.integration.held_tasks_app", "worker",
                "-P", "threads", "-c", "1", "-Q", queue, "-n", self.name, "-l", "warning",
                "--without-gossip", "--without-mingle", "--without-heartbeat",
            ],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def wait_ready(self, celery_app, deadline: float = 30.0):
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            replies = celery_app.control.inspect(destination=[self.name], timeout=0.5).ping() or {}
            if self.name in replies:
                return
            if self.proc.poll() is not None:
                raise RuntimeError(f"worker exited with {self.proc.returncode}")
        raise TimeoutError("worker never answered ping")

    def kill9(self):
        os.kill(self.proc.pid, signal.SIGKILL)
        self.proc.wait(timeout=10)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)


@pytest.fixture
def workers():
    started: list[Worker] = []
    yield started
    for worker in started:
        worker.stop()


def _until(predicate, deadline: float = 30.0, interval: float = 0.25):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise TimeoutError("condition never became true")


def _delayed_flow(broker: str, workers):
    celery_app = _celery(broker)
    queue = f"held-delayed-{uuid.uuid4().hex[:8]}"
    worker = Worker(broker, queue)
    workers.append(worker)
    worker.wait_ready(celery_app)

    result = celery_app.send_task("held.noop", args=["secret-value"], kwargs={"user": "alice"}, queue=queue, countdown=300)
    session_factory = _session_factory(celery_app)
    with session_factory() as session:
        session.add(TaskRun(task_id=result.id, task_name="held.noop", status="queued", args="[]", kwargs="{}"))
        session.commit()

        def listed():
            listing = list_delayed_tasks(celery_app, session, TIMEOUT)
            return next((t for t in listing["tasks"] if t["task_id"] == result.id), None)

        entry = _until(listed)
        assert entry["worker"] == worker.name
        assert entry["task_name"] == "held.noop"
        assert entry["queue"] == queue
        assert entry["eta"] is not None
        assert entry["args_masked"] == {"args": ["str"], "kwargs": {"user": "str"}}
        assert entry["args"] is None  # masked unless revealed
        assert entry["run"]["status"] == "queued"

        outcome = cancel_delayed_tasks(celery_app, session, "tester", [result.id])
        assert outcome == {"revoked": 1, "runs_marked_revoked": 1}
        session.expire_all()
        assert session.query(TaskRun).filter_by(task_id=result.id).one().status == "revoked"
        assert session.query(AuditLog).filter_by(action="cancel", target_id=result.id).count() == 1

    # The worker itself now has the id in its revoked set, so it drops the
    # task at the ETA instead of running it.
    def revoked_on_worker():
        replies = celery_app.control.inspect(destination=[worker.name], timeout=TIMEOUT).revoked() or {}
        return result.id in (replies.get(worker.name) or [])

    _until(revoked_on_worker, deadline=10)
    return celery_app, worker, result


@needs_redis
def test_delayed_task_listed_and_cancelled_on_redis(workers):
    celery_app, worker, result = _delayed_flow(REDIS_URL, workers)
    # On Redis the held ETA message is also in kombu's unacked hash, and the
    # stuck-task view must call it held by the live worker, not orphaned.
    with _session_factory(celery_app)() as session:
        listing = list_unacked(celery_app, session, TIMEOUT)
    entry = next(e for e in listing["entries"] if e["task_id"] == result.id)
    assert entry["state"] == "held"
    assert entry["worker"] == worker.name
    assert entry["worker_state"] == "scheduled"


@needs_amqp
def test_delayed_task_listed_and_cancelled_on_rabbitmq(workers):
    _delayed_flow(AMQP_URL, workers)


@needs_amqp
def test_unacked_view_is_unsupported_on_rabbitmq():
    celery_app = _celery(AMQP_URL)
    with _session_factory(celery_app)() as session:
        listing = list_unacked(celery_app, session, TIMEOUT)
    assert listing["supported"] is False
    assert "RabbitMQ" in listing["reason"]


@needs_redis
@pytest.mark.parametrize("keyprefix", ["", "cmtest:"])
def test_killed_worker_leaves_an_orphan_that_restore_puts_back(workers, keyprefix):
    celery_app = _celery(REDIS_URL, keyprefix)
    queue = f"held-stuck-{uuid.uuid4().hex[:8]}"
    release_key = f"held-release-{uuid.uuid4().hex}"
    session_factory = _session_factory(celery_app)

    first = Worker(REDIS_URL, queue)
    workers.append(first)
    first.wait_ready(celery_app)
    result = celery_app.send_task("held.sleepy", args=[120, release_key], queue=queue)

    def running():
        replies = celery_app.control.inspect(destination=[first.name], timeout=TIMEOUT).active() or {}
        return any(t.get("id") == result.id for t in replies.get(first.name) or [])

    _until(running)

    # While the worker is alive, the message is "running" and restore refuses it.
    with session_factory() as session:
        listing = list_unacked(celery_app, session, TIMEOUT)
        entry = next(e for e in listing["entries"] if e["task_id"] == result.id)
        assert entry["state"] == "running"
        assert entry["queue"] == queue
        assert entry["task_name"] == "held.sleepy"
        refused = restore_unacked(celery_app, session, "tester", [entry["tag"]], TIMEOUT)
        assert refused["restored"] == []
        assert refused["refused"][0]["reason"].startswith("running on")

    first.kill9()

    with session_factory() as session:
        listing = list_unacked(celery_app, session, TIMEOUT)
        entry = next(e for e in listing["entries"] if e["task_id"] == result.id)
        assert entry["state"] == "orphaned"
        assert entry["worker"] is None
        assert entry["past_visibility_timeout"] is False
        assert listing["visibility_timeout_seconds"] == 3600
        assert listing["keys"]["prefix"] == keyprefix

        outcome = restore_unacked(celery_app, session, "tester", [entry["tag"]], TIMEOUT)
        assert outcome["restored"] == [{"tag": entry["tag"], "queue": queue, "task_id": result.id}]
        assert session.query(AuditLog).filter_by(action="restore_unacked").count() == 1

        after = list_unacked(celery_app, session, TIMEOUT)
        assert all(e["task_id"] != result.id for e in after["entries"])

    # Back on the queue: a new worker picks it up and finishes it.
    celery_app.backend.client.set(release_key, "1")
    second = Worker(REDIS_URL, queue)
    workers.append(second)
    second.wait_ready(celery_app)
    assert result.get(timeout=30) == "done"
    celery_app.backend.client.delete(release_key)
