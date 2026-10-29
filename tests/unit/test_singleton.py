"""Distributed ownership and real Celery eager execution against local Redis.

No Docker or downloaded dependency: redis-server must already be installed.
"""
import os
import secrets
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event, Lock

import pytest
from celery import Celery
from fastapi.testclient import TestClient
from redis import Redis
from redis.exceptions import RedisError

from celery_manager import CeleryManagerConfig, setup, track
from celery_manager.api.app import create_app
from celery_manager.api.deps import CurrentUser, get_current_principal
from celery_manager.db.models import AuditLog, TaskRun
from celery_manager.locks import (
    Lease,
    RedisLockProvider,
    SingletonLeaseLost,
    SingletonUnavailable,
)
from celery_manager.singleton import maintain_lease


@pytest.fixture(scope="module")
def redis_client(tmp_path_factory):
    if os.environ.get("CM_TEST_REDIS_URL"):
        client = Redis.from_url(os.environ["CM_TEST_REDIS_URL"], socket_timeout=5)
        client.ping()  # Configured integration gates must fail, never skip.
        try:
            yield client
        finally:
            client.close()
        return
    binary = shutil.which("redis-server")
    if not binary:
        pytest.skip("local redis-server is not installed")
    # macOS Unix socket paths have a small maximum, so use /tmp directly.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="cm-lock-") as directory:
        socket = directory + "/r.sock"
        process = subprocess.Popen([binary, "--port", "0", "--unixsocket", socket,
                                    "--save", "", "--appendonly", "no"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        client = Redis(unix_socket_path=socket, socket_timeout=1)
        try:
            for _ in range(100):
                try:
                    client.ping()
                    break
                except (OSError, ConnectionError, RedisError):
                    if process.poll() is not None:
                        pytest.skip("local Redis server cannot start in this environment")
                    time.sleep(0.02)
            else:
                pytest.fail("local Redis did not become ready")
            yield client
        finally:
            process.terminate()
            process.wait(timeout=5)
            client.close()


class TestOnlyProvider:
    """Deterministic test authority; intentionally never shipped as a backend."""
    __test__ = False

    def __init__(self):
        self.guard, self.rows = Lock(), {}

    def _live(self, key):
        entry = self.rows.get(key)
        if entry and entry[1] > time.monotonic():
            return replace(entry[0], ttl_ms=int((entry[1] - time.monotonic()) * 1000))
        self.rows.pop(key, None)
        return None

    def acquire(self, key, task_id, task_name, ttl):
        with self.guard:
            old = self._live(key)
            if old:
                return False, old
            lease = Lease(key, secrets.token_hex(16), task_id, task_name, "test", int(ttl * 1000))
            self.rows[key] = lease, time.monotonic() + ttl
            return True, lease

    def renew(self, lease, ttl):
        with self.guard:
            old = self._live(lease.key)
            if old is None or old.lease_id != lease.lease_id:
                return False
            self.rows[lease.key] = old, time.monotonic() + ttl
            return True

    def release(self, lease):
        with self.guard:
            old = self._live(lease.key)
            if old is None or old.lease_id != lease.lease_id:
                return False
            del self.rows[lease.key]
            return True

    def inspect(self, key):
        with self.guard:
            return self._live(key)

    def list(self, limit=100):
        with self.guard:
            return [row for key in list(self.rows) if (row := self._live(key))][:limit]


@pytest.fixture
def provider():
    return TestOnlyProvider()


@pytest.fixture
def live_provider(redis_client):
    import uuid
    return RedisLockProvider(client=redis_client, prefix=f"test:{uuid.uuid4()}:")


def test_atomic_contention_and_inspection(provider):
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda i: provider.acquire("one", str(i), "task", 10), range(30)))
    winners = [lease for won, lease in results if won]
    assert len(winners) == 1
    assert {lease.lease_id for _, lease in results} == {winners[0].lease_id}
    assert provider.inspect("one").task_id == winners[0].task_id
    assert provider.list()[0].key == "one"
    assert provider.release(winners[0])
    assert provider.inspect("one") is None


def test_expiry_and_old_owner_cannot_release_or_renew_successor(live_provider, redis_client):
    provider = live_provider
    _, old = provider.acquire("one", "old", "task", 1)
    # Exercise real Redis expiry without adding seconds to the test suite.
    redis_client.pexpire(provider._key("one"), 1)
    time.sleep(0.02)
    won, new = provider.acquire("one", "new", "task", 10)
    assert won and old.lease_id != new.lease_id
    assert not provider.release(old)
    assert not provider.renew(old, 20)
    assert provider.renew(new, 20)
    assert provider.inspect("one").ttl_ms > 10000


def app_task(tmp_path, provider, name="singleton_test", **options):
    app = Celery(name, broker="memory://", backend="cache+memory://")
    config = CeleryManagerConfig(celery_app=app, db_url=f"sqlite:///{tmp_path / (name + '.db')}",
                                lock_provider=provider, secret_key="test-only-secret")
    sessions = setup(config)
    calls = []
    @app.task(name=name + ".run", shared=False)
    @track(singleton="resource", **options)
    def task(value):
        calls.append(value)
        return value
    return task, calls, sessions, config


def test_conflict_skips_and_records_owner(provider, tmp_path):
    task, calls, sessions, _ = app_task(tmp_path, provider)
    _, owner = provider.acquire("resource", "other-worker", "other.task", 10)
    assert task.apply(args=[4], task_id="blocked").get() is None
    assert calls == []
    with sessions() as db:
        run = db.query(TaskRun).one()
        assert run.status == "skipped" and "other-worker" in run.skip_reason
    provider.release(owner)
    assert task.apply(args=[5], task_id="allowed").get() == 5
    assert calls == [5] and provider.inspect("resource") is None


def test_retry_conflict_never_executes_body(provider, tmp_path):
    from celery.exceptions import Retry
    task, calls, _, _ = app_task(tmp_path, provider, on_conflict="retry")
    provider.acquire("resource", "owner", "task", 10)
    # throw=True exposes Celery's actual Retry rather than eagerly executing
    # every retry immediately as apply() otherwise does.
    with pytest.raises(Retry):
        task.apply(args=[4], task_id="blocked", throw=True)
    assert calls == []


def test_redis_outage_fails_closed_and_unconfigured_broker_is_explicit(tmp_path):
    class Unavailable:
        def acquire(self, *args):
            raise ConnectionError("offline")
    task, calls, sessions, _ = app_task(tmp_path, Unavailable())
    with pytest.raises(ConnectionError):
        task.apply(args=[1], throw=True)
    assert calls == []
    with sessions() as db:
        assert db.query(TaskRun).one().status == "failed"
    task2, calls2, _, _ = app_task(tmp_path, None, name="no_lock_provider")
    with pytest.raises(SingletonUnavailable):
        task2.apply(args=[1], throw=True)
    assert calls2 == []


def test_concurrent_eager_tasks_execute_only_one_body(provider, tmp_path):
    app = Celery("concurrent_singleton", broker="memory://", backend="cache+memory://")
    setup(CeleryManagerConfig(celery_app=app, db_url=f"sqlite:///{tmp_path / 'concurrent.db'}", lock_provider=provider))
    entered, finish = Event(), Event()
    @app.task(name="concurrent_singleton.run", shared=False)
    @track(singleton=True, singleton_ttl=1)
    def task():
        entered.set()
        assert finish.wait(5)
        return "executed"
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(lambda: task.apply(task_id="first").get())
        assert entered.wait(3)
        # Wait past the original TTL: renewal must keep the lease alive.
        time.sleep(1.2)
        assert task.apply(task_id="second").get() is None
        finish.set()
        assert first.result(timeout=3) == "executed"


def test_two_apps_use_distinct_lock_authorities(provider, tmp_path):
    second = TestOnlyProvider()
    a = app_task(tmp_path, provider, name="singleton_app_a")
    b = app_task(tmp_path, second, name="singleton_app_b")
    provider.acquire("resource", "owner", "task", 10)
    assert a[0].apply(args=[1]).get() is None
    assert b[0].apply(args=[2]).get() == 2
    assert a[1] == [] and b[1] == [2]


def test_lost_lease_is_visible_and_original_body_error_is_preserved(provider):
    _, lease = provider.acquire("resource", "owner", "task", 10)
    with pytest.raises(SingletonLeaseLost), maintain_lease(provider, lease, 10):
        provider.release(lease)
    _, lease = provider.acquire("resource", "owner", "task", 10)
    with pytest.raises(ValueError, match="body failed"), maintain_lease(provider, lease, 10):
        provider.release(lease)
        raise ValueError("body failed")


def test_inspection_roles_conditional_release_and_audit(provider, tmp_path):
    _, _, _, config = app_task(tmp_path, provider)
    app, sessions = create_app(config)
    role = ["viewer"]
    app.dependency_overrides[get_current_principal] = lambda: CurrentUser("alice", role[0], subject="host:alice")
    client = TestClient(app, base_url="https://testserver")
    _, lease = provider.acquire("resource", "owner", "task", 10)
    assert client.get("/api/locks").json()["locks"][0]["task_id"] == "owner"
    for r in ("viewer", "operator"):
        role[0] = r
        assert client.post("/api/locks/release", json={"key":"resource", "lease_id":lease.lease_id}).status_code == 403
    role[0] = "admin"
    assert client.post("/api/locks/release", json={"key":"resource", "lease_id":"stale"}).status_code == 409
    assert provider.inspect("resource") is not None
    assert client.post("/api/locks/release", json={"key":"resource", "lease_id":lease.lease_id}).status_code == 200
    assert provider.inspect("resource") is None
    with sessions() as db:
        rows = db.query(AuditLog).filter(AuditLog.target_type == "singleton_lock").all()
        assert len(rows) == 3 and rows[-1].action == "singleton_release"
        assert all(row.actor_subject == "host:alice" for row in rows)


@pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan"), True])
def test_invalid_ttl_rejected_at_decoration(ttl):
    with pytest.raises(ValueError):
        track(singleton=True, singleton_ttl=ttl)


def test_real_redis_atomic_contention(live_provider):
    test_atomic_contention_and_inspection(live_provider)
