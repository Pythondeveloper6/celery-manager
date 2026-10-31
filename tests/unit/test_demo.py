"""`celery-manager demo`: seeded history the real dashboard API can serve."""
import random
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from celery_manager import demo
from celery_manager.api.app import create_app


@pytest.fixture
def site(tmp_path):
    return demo.build(tmp_path / "site", rng=random.Random(1))


@pytest.fixture
def client(site):
    app, _ = create_app(demo.demo_config(site))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/api/auth/login", json={"username": demo.DEMO_USER, "password": site.password})
        assert response.status_code == 200, response.text
        yield client


def test_build_seeds_runs_and_a_random_admin_password(tmp_path):
    first = demo.build(tmp_path / "a")
    second = demo.build(tmp_path / "b")
    assert first.runs > 300
    assert first.password != second.password and len(first.password) >= 12
    assert (tmp_path / "a" / "demo.db").is_file()


def test_build_refuses_to_reuse_an_existing_database(tmp_path, site):
    with pytest.raises(FileExistsError):
        demo.build(site.directory)


def test_dashboard_lists_seeded_failures_and_the_running_task(client):
    failed = client.get("/api/tasks", params={"status": "failed"}).json()
    items = failed["items"] if isinstance(failed, dict) else failed
    assert items and all(item["status"] == "failed" for item in items)
    running = client.get("/api/tasks/demo-running").json()
    assert running["status"] == "started" and running["progress_current"] == 640


def test_demo_app_exposes_its_periodic_schedules(client):
    body = client.get("/api/schedules").json()
    names = {item["name"] for item in (body["items"] if isinstance(body, dict) else body)}
    assert {"daily-report", "catalog-sync", "cache-refresh", "nightly-digest"} <= names


def test_run_serves_with_uvicorn_and_prints_the_login(tmp_path, capsys):
    pytest.importorskip("uvicorn")
    with patch("uvicorn.run") as serve:
        assert demo.run(tmp_path / "served", port=9999, open_browser=False) == 0
    serve.assert_called_once()
    assert serve.call_args.kwargs["port"] == 9999 and serve.call_args.kwargs["host"] == "127.0.0.1"
    out = capsys.readouterr().out
    assert "sign in as 'demo'" in out and str(tmp_path / "served") in out


def test_cli_dispatches_demo(tmp_path):
    from celery_manager.cli import main

    with patch("celery_manager.demo.run", return_value=0) as run, pytest.raises(SystemExit) as exit_:
        main(["demo", "--dir", str(tmp_path / "x"), "--port", "9000", "--no-browser"])
    assert exit_.value.code == 0
    assert run.call_args.kwargs == {"host": "127.0.0.1", "port": 9000, "open_browser": False}
