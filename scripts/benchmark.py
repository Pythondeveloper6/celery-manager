"""Measure celery-manager's own overhead on your hardware and database.

    python scripts/benchmark.py                       # SQLite in a temp dir
    python scripts/benchmark.py --db-url postgresql+psycopg://...  # an empty database

Two numbers matter when deciding whether celery-manager can keep up:

* listener: how many Celery task events per second the events listener can
  store (each task produces 3-4 events: received, started, succeeded/failed).
* dashboard: the latency of the API calls the dashboard makes most often,
  against a seeded history.

The figures depend on the database far more than on celery-manager; run this
against the database you plan to use. Nothing here talks to a broker.
"""
from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
import uuid
from pathlib import Path


def _listener(session_factory, events: int) -> dict:
    from celery_manager.events.listener import upsert_task_run

    now = time.time()
    tasks = max(1, events // 3)
    stream = []
    for i in range(tasks):
        task_id = str(uuid.uuid4())
        stream += [
            {"type": "task-received", "uuid": task_id, "name": "bench.task", "args": "[1]", "kwargs": "{}",
             "timestamp": now + i, "hostname": "worker@bench", "routing_key": "default"},
            {"type": "task-started", "uuid": task_id, "timestamp": now + i + 0.01, "hostname": "worker@bench"},
            {"type": "task-succeeded", "uuid": task_id, "timestamp": now + i + 0.05, "runtime": 0.04,
             "result": "'ok'", "hostname": "worker@bench"},
        ]
    started = time.perf_counter()
    for event in stream:
        with session_factory() as session:
            upsert_task_run(session, event)
            session.commit()
    elapsed = time.perf_counter() - started
    return {"events": len(stream), "seconds": round(elapsed, 3), "events_per_second": round(len(stream) / elapsed)}


def _dashboard(config, requests: int) -> dict:
    from fastapi.testclient import TestClient

    from celery_manager.api.app import create_app
    from celery_manager.demo import DEMO_USER

    app, _ = create_app(config)
    client = TestClient(app, base_url="https://bench")
    login = client.post("/api/auth/login", json={"username": DEMO_USER, "password": config._bench_password})
    login.raise_for_status()
    results = {}
    for path in ("/api/tasks?limit=50", "/api/tasks?q=sync&limit=50", "/api/analytics/overview", "/api/analytics/failures"):
        timings = []
        for _ in range(requests):
            started = time.perf_counter()
            response = client.get(path)
            timings.append((time.perf_counter() - started) * 1000)
            response.raise_for_status()
        timings.sort()
        results[path] = {"p50_ms": round(statistics.median(timings), 1),
                         "p95_ms": round(timings[int(len(timings) * 0.95) - 1], 1)}
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db-url", help="an empty database; default: SQLite in a temporary directory")
    parser.add_argument("--events", type=int, default=3000)
    parser.add_argument("--requests", type=int, default=50)
    args = parser.parse_args(argv)

    import secrets

    from celery_manager.auth.users import create_user
    from celery_manager.config import CeleryManagerConfig
    from celery_manager.db.session import get_session_factory, init_db
    from celery_manager.demo import DEMO_USER, demo_celery_app, seed

    db_url = args.db_url or f"sqlite:///{Path(tempfile.mkdtemp(prefix='cm-bench-')) / 'bench.db'}"
    config = CeleryManagerConfig(celery_app=demo_celery_app(), db_url=db_url, secret_key=secrets.token_hex(32))
    engine = init_db(config)
    factory = get_session_factory(engine)
    password = secrets.token_urlsafe(12)
    with factory() as session:
        create_user(session, DEMO_USER, password, role="viewer")
        seeded = seed(session)
    report = {"db": engine.dialect.name, "seeded_runs": seeded,
              "listener": _listener(factory, args.events)}
    config._bench_password = password
    report["dashboard"] = _dashboard(config, args.requests)
    engine.dispose()
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
