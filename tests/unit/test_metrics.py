"""The /metrics endpoint: auth, exposition format, caching, degradation."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from celery import Celery
from fastapi import FastAPI
from fastapi.testclient import TestClient

import celery_manager.metrics as metrics_module
from celery_manager.api.app import create_app
from celery_manager.config import CeleryManagerConfig
from celery_manager.db.models import TaskRun
from celery_manager.metrics import (
    MAX_TASK_LABELS,
    METRIC_NAMES,
    MetricsCollector,
    compute_snapshot,
    render,
)

TOKEN = "s3cret-scrape-token"

_SAMPLE = re.compile(
    r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>.*)\})? (?P<value>\S+)$'
)
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse_exposition(text: str) -> dict[str, list[tuple[dict[str, str], float]]]:
    """A strict-enough reader of the text format: every sample must follow its
    family's TYPE line, labels must be well quoted, values must be numbers."""
    families: dict[str, list] = {}
    typed: set[str] = set()
    assert text.endswith("\n")
    for line in text.splitlines():
        if line.startswith("# HELP "):
            continue
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split(" ")
            assert kind in {"gauge", "counter", "histogram", "summary", "untyped"}
            assert name not in typed, f"TYPE repeated for {name}"
            typed.add(name)
            families[name] = []
            continue
        match = _SAMPLE.match(line)
        assert match, f"unparseable line: {line!r}"
        assert match["name"] in typed, f"sample before TYPE: {line!r}"
        labels = {}
        if match["labels"]:
            consumed = ",".join(f'{k}="{v}"' for k, v in _LABEL.findall(match["labels"]))
            assert consumed == match["labels"], f"bad labels: {line!r}"
            labels = {k: v for k, v in _LABEL.findall(match["labels"])}
        families[match["name"]].append((labels, float(match["value"].replace("+Inf", "inf"))))
    return families


@pytest.fixture(autouse=True)
def _no_real_broker_probe(monkeypatch):
    # These tests have no broker; without this every snapshot would spend its
    # connect timeout on the broker-health probe. Broker metrics have their
    # own tests in test_broker_health.py.
    monkeypatch.setattr(
        metrics_module, "broker_health",
        lambda celery_app: {"transport": "memory", "reachable": True, "latency_ms": 1.0, "details": None},
    )


def make_app(token=TOKEN, cache=0.0, celery_app=None):
    config = CeleryManagerConfig(
        celery_app=celery_app or Celery("t"),
        db_url="sqlite:///:memory:",
        secret_key="test-secret",
        metrics_token=token,
        metrics_cache_seconds=cache,
        inspect_timeout_seconds=0.01,
    )
    app, session_factory = create_app(config)
    return app, session_factory, config


def add_run(session_factory, task_id, name="demo.a", status="succeeded", runtime=1.0, finished_ago=60):
    with session_factory() as s:
        s.add(
            TaskRun(
                task_id=task_id,
                task_name=name,
                status=status,
                runtime_seconds=runtime,
                finished_at=datetime.now(timezone.utc) - timedelta(seconds=finished_ago),
            )
        )
        s.commit()


@pytest.fixture(autouse=True)
def quiet_broker(monkeypatch):
    """No real broker in unit tests: queues answer quickly with nothing."""
    monkeypatch.setattr(
        metrics_module,
        "list_queues",
        lambda *a, **k: {"broker": {"error": None}, "queues": [{"name": "celery", "depth": 7, "recorded_queued": 2}]},
    )
    # Pinging a worker needs a broker; an unreachable default one would stall every test.
    monkeypatch.setattr(
        metrics_module,
        "_workers_family",
        lambda *a, **k: metrics_module.Family("celery_manager_workers_online", "w", [({}, 2)]),
    )


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


# --- auth --------------------------------------------------------------------


def test_disabled_without_a_token_is_404():
    app, _, _ = make_app(token=None)
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/metrics").status_code == 404
    assert client.get("/metrics", headers=auth()).status_code == 404


def test_missing_or_wrong_token_is_401():
    app, _, _ = make_app()
    client = TestClient(app, base_url="https://testserver")
    for headers in ({}, auth("nope"), {"Authorization": TOKEN}, {"Authorization": f"Basic {TOKEN}"}, {"Authorization": "Bearer "}):
        response = client.get("/metrics", headers=headers)
        assert response.status_code == 401, headers
        assert response.headers["www-authenticate"] == "Bearer"


def test_session_cookie_is_not_accepted_in_place_of_the_token():
    app, _, _ = make_app()
    client = TestClient(app, base_url="https://testserver")
    client.cookies.set("cm_session", "anything")
    assert client.get("/metrics").status_code == 401


def test_correct_token_returns_text_exposition_and_only_get():
    app, _, _ = make_app()
    client = TestClient(app, base_url="https://testserver")
    response = client.get("/metrics", headers=auth())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    # CSRF/Origin checks never apply to a GET; a POST is simply not allowed.
    assert client.post("/metrics", headers=auth()).status_code == 405


def test_works_when_mounted_under_a_prefix():
    app, _, _ = make_app()
    host = FastAPI()
    host.mount("/celery", app)
    client = TestClient(host, base_url="https://testserver")
    assert client.get("/celery/metrics", headers=auth()).status_code == 200


# --- content -----------------------------------------------------------------


def test_exposition_parses_and_reports_runs_ratio_and_runtimes():
    app, session_factory, _ = make_app()
    add_run(session_factory, "1", runtime=0.05)
    add_run(session_factory, "2", runtime=2.0)
    add_run(session_factory, "3", status="failed", runtime=20.0)
    add_run(session_factory, "4", runtime=1.0, finished_ago=3 * 3600)  # outside the window
    client = TestClient(app, base_url="https://testserver")
    families = parse_exposition(client.get("/metrics", headers=auth()).text)

    stored = {(l["task"], l["status"]): v for l, v in families["celery_manager_task_runs"]}
    assert stored[("demo.a", "succeeded")] == 3 and stored[("demo.a", "failed")] == 1
    window = {(l["task"], l["status"]): v for l, v in families["celery_manager_task_runs_finished_window"]}
    assert window[("demo.a", "succeeded")] == 2 and window[("demo.a", "failed")] == 1
    assert families["celery_manager_task_failure_ratio_window"] == [({"task": "demo.a"}, pytest.approx(1 / 3))]

    buckets = {l["le"]: v for l, v in families["celery_manager_task_runtime_window_seconds_bucket"]}
    assert buckets["0.1"] == 1 and buckets["2.5"] == 2 and buckets["30.0"] == 3 and buckets["+Inf"] == 3
    assert families["celery_manager_task_runtime_window_seconds_sum"][0][1] == pytest.approx(22.05)
    assert families["celery_manager_task_runtime_window_seconds_count"][0][1] == 3
    assert families["celery_manager_queue_depth"] == [({"queue": "celery"}, 7.0)]
    assert families["celery_manager_collector_up"]
    assert set(families) <= set(METRIC_NAMES)


def test_task_labels_are_bounded():
    _app, session_factory, config = make_app()
    with session_factory() as s:
        for i in range(MAX_TASK_LABELS + 30):
            s.add(TaskRun(task_id=f"t{i}", task_name=f"gen.task{i}", status="succeeded"))
        s.commit()
    families = parse_exposition(render(compute_snapshot(config, session_factory)))
    tasks = {l["task"] for l, _ in families["celery_manager_task_runs"]}
    assert len(tasks) <= MAX_TASK_LABELS + 1 and "other" in tasks
    assert sum(v for _, v in families["celery_manager_task_runs"]) == MAX_TASK_LABELS + 30


def test_label_values_are_escaped():
    text = render([metrics_module.Family("celery_manager_x", "h", [({"task": 'a"b\\c\nd'}, 1)])])
    assert 'task="a\\"b\\\\c\\nd"' in text
    parse_exposition(text)


def test_missed_runs_listed_per_beat_schedule():
    celery_app = Celery("t")
    celery_app.conf.beat_schedule = {"nightly": {"task": "demo.a", "schedule": 3600.0}}
    _app, session_factory, config = make_app(celery_app=celery_app)
    families = parse_exposition(render(compute_snapshot(config, session_factory)))
    assert families["celery_manager_schedule_missed_runs_24h"] == [({"schedule": "nightly"}, 0.0)]


# --- caching and degradation -------------------------------------------------


def test_cache_is_honoured(monkeypatch):
    app, _session_factory, _config = make_app(cache=60.0)
    calls = []
    real = metrics_module.compute_snapshot
    monkeypatch.setattr(metrics_module, "compute_snapshot", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    client = TestClient(app, base_url="https://testserver")
    for _ in range(3):
        assert client.get("/metrics", headers=auth()).status_code == 200
    assert len(calls) == 1


def test_cache_expires_and_zero_disables_it(monkeypatch):
    _app, session_factory, config = make_app(cache=5.0)
    now = [100.0]
    calls = []
    monkeypatch.setattr(metrics_module, "compute_snapshot", lambda *a, **k: calls.append(1) or [])
    collector = MetricsCollector(config, session_factory, clock=lambda: now[0])
    collector.snapshot()
    collector.snapshot()
    now[0] += 6
    collector.snapshot()
    assert len(calls) == 2
    config.metrics_cache_seconds = 0
    collector.snapshot()
    collector.snapshot()
    assert len(calls) == 4


def test_broker_failure_omits_queue_and_worker_metrics_instead_of_500(monkeypatch):
    app, session_factory, _config = make_app()
    add_run(session_factory, "1")
    monkeypatch.setattr(
        metrics_module,
        "list_queues",
        lambda *a, **k: {
            "broker": {"error": "could not reach the broker"},
            "queues": [{"name": "celery", "depth": None, "recorded_queued": 2}],
        },
    )

    def boom(*a, **k):
        raise OSError("broker down")

    monkeypatch.setattr(metrics_module, "_workers_family", boom)
    client = TestClient(app, base_url="https://testserver")
    response = client.get("/metrics", headers=auth())
    assert response.status_code == 200
    families = parse_exposition(response.text)
    assert "celery_manager_queue_depth" not in families
    assert "celery_manager_workers_online" not in families
    up = {l["collector"]: v for l, v in families["celery_manager_collector_up"]}
    assert up["queues"] == 0 and up["workers"] == 0 and up["runs"] == 1
    # Database-backed metrics still there, recorded-queued included.
    assert families["celery_manager_task_runs"]
    assert families["celery_manager_queue_recorded_queued"] == [({"queue": "celery"}, 2.0)]


def test_list_queues_raising_also_degrades(monkeypatch):
    app, _, _ = make_app()

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(metrics_module, "list_queues", boom)
    assert TestClient(app, base_url="https://testserver").get("/metrics", headers=auth()).status_code == 200


# --- OpenTelemetry -----------------------------------------------------------


def test_otel_register_without_the_api_gives_a_clear_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("opentelemetry"):
            raise ImportError("no otel")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    from celery_manager.metrics import otel

    with pytest.raises(ImportError, match=r"celery-manager\[otel\]"):
        otel.register(collector=object())


def test_otel_gauges_report_the_same_snapshot():
    pytest.importorskip("opentelemetry.metrics")
    from celery_manager.metrics import otel

    app, session_factory, _ = make_app(cache=60.0)
    add_run(session_factory, "1")
    registered = {}

    class Meter:
        def create_observable_gauge(self, name, callbacks=(), **kwargs):
            registered[name] = callbacks

    class Provider:
        def get_meter(self, name):
            return Meter()

    otel.register(Provider(), app=app)
    assert set(registered) == set(METRIC_NAMES)
    observations = registered["celery_manager_task_runs"][0](None)
    assert [(o.attributes, o.value) for o in observations] == [({"task": "demo.a", "status": "succeeded"}, 1)]
    with pytest.raises(ValueError):
        otel.register(Provider())
