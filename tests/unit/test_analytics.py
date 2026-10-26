from datetime import datetime, timedelta, timezone

import pytest
from celery import Celery
from fastapi.testclient import TestClient

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.control import analytics
from celery_manager.db.models import TaskRun

NOW = datetime(2026, 8, 15, 12, 30, 0, tzinfo=timezone.utc)

TB = '''Traceback (most recent call last):
  File "/app/.venv/lib/python3.12/site-packages/celery/app/trace.py", line 453, in trace_task
    R = retval = fun(*args, **kwargs)
  File "/app/tasks.py", line 41, in send_report
    raise ConnectionError("x")
ConnectionError: x'''


def make_run(i, name="t.a", status="succeeded", age=timedelta(minutes=10), runtime=1.0, exception=None, traceback=None, **kw):
    started = kw.pop("started_at", NOW - age)
    return TaskRun(
        task_id=f"id-{i}", task_name=name, status=status, started_at=started,
        finished_at=started + timedelta(seconds=runtime or 0), runtime_seconds=runtime,
        exception=exception, traceback=traceback, **kw,
    )


@pytest.fixture
def env():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
    return app, session_factory


def seed(session_factory, runs):
    with session_factory() as session:
        session.add_all(runs)
        session.commit()


def test_percentile_interpolates():
    assert analytics.percentile([], 50) is None
    assert analytics.percentile([5.0], 95) == 5.0
    assert analytics.percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert analytics.percentile([0.0, 10.0], 95) == pytest.approx(9.5)


def test_overview_buckets_and_totals(env):
    _, sf = env
    runs = [
        make_run(1, status="succeeded", age=timedelta(minutes=10), runtime=1.0),
        make_run(2, status="succeeded", age=timedelta(minutes=12), runtime=3.0),
        make_run(3, status="failed", age=timedelta(minutes=14), runtime=5.0),
        make_run(4, status="skipped", age=timedelta(minutes=15), runtime=None),
        make_run(5, status="succeeded", age=timedelta(hours=5), runtime=2.0),
        make_run(6, status="succeeded", age=timedelta(hours=30), runtime=2.0),  # outside 24h
    ]
    # Queued: no timestamps at all -> cannot be placed on a timeline.
    runs.append(TaskRun(task_id="id-q", task_name="t.a", status="queued"))
    seed(sf, runs)
    with sf() as s:
        out = analytics.overview(s, "24h", now=NOW)

    assert out["bucket_seconds"] == 3600
    assert out["totals"]["runs"] == 5
    assert out["totals"]["failed"] == 1
    assert out["totals"]["success_rate"] == 0.75  # 3 / (3 + 1); skipped excluded
    assert out["totals"]["runtime_p50"] == 2.5  # 1,2,3,5 -> 2.5
    assert out["totals"]["throughput_per_hour"] == round(5 / 24, 2)
    # Window is 24h aligned to hours: 12:30 -> buckets 12:00 back to 12:00 yesterday.
    assert len(out["buckets"]) == 25
    last = out["buckets"][-1]
    assert last["start"].startswith("2026-08-15T12:00:00")
    assert last["counts"] == {"succeeded": 2, "failed": 1, "skipped": 1}
    assert last["success_rate"] == pytest.approx(0.6667, abs=1e-4)
    assert sum(b["total"] for b in out["buckets"]) == 5


@pytest.mark.parametrize("rng,size,count", [("1h", 300, 13), ("24h", 3600, 25), ("7d", 21600, 29), ("30d", 86400, 31)])
def test_bucket_sizes_per_range(env, rng, size, count):
    _, sf = env
    with sf() as s:
        out = analytics.overview(s, rng, now=NOW)
    assert out["bucket_seconds"] == size
    assert len(out["buckets"]) == count
    assert all(b["total"] == 0 and b["runtime_p95"] is None for b in out["buckets"])


def test_started_at_falls_back_to_finished_at(env):
    _, sf = env
    seed(sf, [TaskRun(task_id="x", task_name="t.a", status="revoked", finished_at=NOW - timedelta(minutes=3))])
    with sf() as s:
        assert analytics.overview(s, "1h", now=NOW)["totals"]["runs"] == 1


def test_task_filter_and_top_tasks(env):
    _, sf = env
    runs = [make_run(i, name="t.big", status="succeeded") for i in range(6)]
    runs += [make_run(10 + i, name="t.flaky", status="failed" if i < 2 else "succeeded") for i in range(4)]
    runs.append(make_run(30, name="t.once", status="failed"))
    seed(sf, runs)
    with sf() as s:
        out = analytics.overview(s, "24h", now=NOW)
        scoped = analytics.overview(s, "24h", task_name="t.flaky", now=NOW)
    assert [t["task_name"] for t in out["top_by_volume"]] == ["t.big", "t.flaky", "t.once"]
    # t.once has a 100% failure rate but only one run: below the minimum.
    assert [t["task_name"] for t in out["top_by_failure_rate"]] == ["t.flaky"]
    assert out["top_by_failure_rate"][0]["failure_rate"] == 0.5
    assert scoped["totals"]["runs"] == 4 and scoped["top_by_volume"] == []


def test_unknown_range():
    with pytest.raises(analytics.UnknownRange):
        analytics.overview(None, "2d")


@pytest.mark.parametrize("exception,expected", [
    ("ConnectionError('SMTP relay smtp.internal:587 refused')", "ConnectionError: SMTP relay smtp.internal:<n> refused"),
    ("ConnectionError('SMTP relay smtp.internal:25 refused')", "ConnectionError: SMTP relay smtp.internal:<n> refused"),
    ("KeyError('user_8812')", "KeyError: user_<n>"),
    ("ValueError: invalid id 550e8400-e29b-41d4-a716-446655440000", "ValueError: invalid id <uuid>"),
    ("RuntimeError('object at 0x7f3a2c1b9d50 busy')", "RuntimeError: object at <addr> busy"),
    ("TimeoutError('job a1b2c3d4e5f6 timed out after 30.5s')", "TimeoutError: job <hex> timed out after <n>s"),
    ("ValueError(\"bad value 'abc' for field\")", "ValueError: bad value <str> for field"),
    ("something odd happened 42 times", "Exception: something odd happened <n> times"),
])
def test_signature_normalisation(exception, expected):
    assert analytics.failure_signature(exception, None)[0] == expected


def test_signature_includes_last_frame():
    sig, exc_type, _msg, frame = analytics.failure_signature("ConnectionError('x')", TB)
    assert frame == "tasks.py:send_report"
    assert sig == "ConnectionError: x @ tasks.py:send_report"
    assert exc_type == "ConnectionError"


def test_failures_grouping(env):
    _, sf = env
    runs = [
        make_run(1, "t.a", "failed", timedelta(minutes=5), exception="ConnectionError('relay 10.0.0.1:587 refused')", traceback=TB),
        make_run(2, "t.b", "failed", timedelta(minutes=30), exception="ConnectionError('relay 10.0.0.2:25 refused')", traceback=TB),
        make_run(3, "t.a", "failed", timedelta(minutes=45), exception="ConnectionError('relay 10.0.0.3:25 refused')", traceback=TB),
        make_run(4, "t.a", "failed", timedelta(minutes=50), exception="KeyError('user_1')"),
        make_run(5, "t.a", "succeeded", timedelta(minutes=5)),
    ]
    seed(sf, runs)
    with sf() as s:
        out = analytics.failures(s, "24h", now=NOW)
        scoped = analytics.failures(s, "24h", task_name="t.b", now=NOW)
    assert out["total_failures"] == 4
    top = out["groups"][0]
    assert top["count"] == 3
    assert top["frame"] == "tasks.py:send_report"
    assert top["task_names"] == ["t.a", "t.b"]
    assert top["example_task_ids"] == ["id-3", "id-2", "id-1"]  # newest row first
    assert top["first_seen"] < top["last_seen"]
    assert top["example"].startswith("ConnectionError('relay 10.0.0.3")
    assert out["groups"][1]["count"] == 1
    assert [g["count"] for g in scoped["groups"]] == [1]


def test_failures_task_ids_capped(env):
    _, sf = env
    seed(sf, [make_run(i, "t.a", "failed", timedelta(minutes=1), exception="Boom('x')") for i in range(analytics.MAX_GROUP_TASK_IDS + 5)])
    with sf() as s:
        g = analytics.failures(s, "24h", now=NOW)["groups"][0]
    assert g["count"] == analytics.MAX_GROUP_TASK_IDS + 5
    assert len(g["task_ids"]) == analytics.MAX_GROUP_TASK_IDS and g["task_ids_truncated"]


def test_task_stats(env):
    _, sf = env
    seed(sf, [
        make_run(1, "t.a", "succeeded", timedelta(days=2), runtime=2.0),
        make_run(2, "t.a", "succeeded", timedelta(hours=1), runtime=4.0),
        make_run(3, "t.a", "failed", timedelta(minutes=5), runtime=9.0, exception="Boom('x')"),
        make_run(4, "t.other", "succeeded", timedelta(minutes=5)),
    ])
    with sf() as s:
        out = analytics.task_stats(s, "t.a", "30d", now=NOW)
        assert analytics.task_stats(s, "nope", "30d", now=NOW) is None
    assert out["totals"]["runs"] == 3
    assert out["totals"]["success_rate"] == pytest.approx(0.6667, abs=1e-4)
    assert out["runtime"] == {"p50": 4.0, "p95": 8.5, "max": 9.0}
    assert out["last_failure_at"].startswith("2026-08-15T12:25")
    assert out["last_success_at"].startswith("2026-08-15T11:30")
    assert [r.task_id for r in out["recent_runs"]] == ["id-3", "id-2", "id-1"]
    assert out["top_errors"][0]["count"] == 1


# --- HTTP ---

def logged_in(app):
    client = TestClient(app, base_url="https://testserver")
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    return client


@pytest.mark.parametrize("path", [
    "/api/analytics/overview", "/api/analytics/failures", "/api/analytics/tasks/t.a",
])
def test_endpoints_require_auth(env, path):
    app, _ = env
    assert TestClient(app, base_url="https://testserver").get(path).status_code == 401


def test_endpoints_roundtrip(env):
    app, sf = env
    now = datetime.now(timezone.utc)
    seed(sf, [
        TaskRun(task_id="a", task_name="t.a", status="failed", started_at=now - timedelta(minutes=5),
                runtime_seconds=1.5, exception="Boom('x')"),
        TaskRun(task_id="b", task_name="t.a", status="succeeded", started_at=now - timedelta(minutes=6), runtime_seconds=0.5),
    ])
    client = logged_in(app)

    ov = client.get("/api/analytics/overview", params={"range": "1h"}).json()
    assert ov["totals"]["runs"] == 2 and ov["buckets"][0]["start"].endswith("+00:00")
    assert client.get("/api/analytics/overview", params={"range": "2d"}).status_code == 422

    fl = client.get("/api/analytics/failures", params={"range": "24h", "task_name": "t.a"}).json()
    assert fl["groups"][0]["signature"] == "Boom: x"

    ts = client.get("/api/analytics/tasks/t.a").json()
    assert ts["totals"]["runs"] == 2
    assert ts["recent_runs"][0]["started_at"].endswith("+00:00")
    assert client.get("/api/analytics/tasks/missing").status_code == 404
