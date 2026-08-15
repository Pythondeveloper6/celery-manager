# tests/unit/test_setup.py
#
# C2 regression: `configure()` was only ever called from `create_app()`, which
# runs in the API process — never in the Celery worker process, where
# @track-decorated tasks actually execute. So in any real deployment the
# worker's `_session_factory` stayed None and @track silently no-opped, taking
# the pause/skip feature and log capture with it. These tests cover the new
# `celery_manager.setup(config)` entry point that the worker is now documented
# to call, going through the public API rather than `configure()` directly.
import logging

import pytest
from celery import Celery

import celery_manager
from celery_manager import decorator as decorator_module
from celery_manager.config import CeleryManagerConfig
from celery_manager.db.models import TaskRun


@pytest.fixture
def celery_app():
    app = Celery("setup_test_app", broker="memory://", backend="cache+memory://")
    app.conf.task_always_eager = True
    return app


@pytest.fixture(autouse=True)
def unconfigured_decorator(monkeypatch):
    """Start each test from a genuinely unconfigured process, the way a fresh
    Celery worker starts — otherwise another test's `configure()` would make
    these pass vacuously."""
    monkeypatch.setattr(decorator_module, "_session_factory", None)
    monkeypatch.setattr(decorator_module, "_warned_unconfigured", False)


def test_public_api_is_importable_from_the_package_root():
    """Spec §4 assumes `celery_manager.track` / `celery_manager.control.*` as
    the public surface; the package root used to export only __version__."""
    assert celery_manager.track is decorator_module.track
    assert callable(celery_manager.setup)
    assert celery_manager.CeleryManagerConfig is CeleryManagerConfig
    assert callable(celery_manager.control.list_task_runs)


def test_setup_wires_the_decorator_so_a_tracked_task_persists_a_run(celery_app):
    config = CeleryManagerConfig(celery_app=celery_app, db_url="sqlite:///:memory:", secret_key="s")

    session_factory = celery_manager.setup(config)

    @celery_app.task(name="setup_demo.echo", bind=True)
    @celery_manager.track()
    def echo(self, value):
        return value

    result = echo.apply(args=["hi"], task_id="setup-uuid-1")
    assert result.state == "SUCCESS"

    with session_factory() as session:
        run = session.query(TaskRun).filter_by(task_id="setup-uuid-1").one()
        assert run.task_name == "setup_demo.echo"
        assert run.status == "succeeded"
        # Log capture — the other thing an unconfigured worker silently loses.
        assert run.log_output is not None


def test_setup_is_idempotent_for_the_same_config(celery_app):
    config = CeleryManagerConfig(celery_app=celery_app, db_url="sqlite:///:memory:", secret_key="s")

    first = celery_manager.setup(config)
    second = celery_manager.setup(config)

    # Same session factory, i.e. the same engine — not a second engine over a
    # second (for :memory:, entirely separate) database.
    assert first is second
    assert decorator_module._session_factory is first


def test_setup_with_a_different_config_repoints_the_decorator(celery_app):
    config_a = CeleryManagerConfig(celery_app=celery_app, db_url="sqlite:///:memory:", secret_key="s")
    config_b = CeleryManagerConfig(
        celery_app=celery_app, db_url="sqlite:///:memory:", secret_key="s", eta_tolerance_seconds=5
    )

    factory_a = celery_manager.setup(config_a)
    factory_b = celery_manager.setup(config_b)

    assert factory_a is not factory_b
    assert decorator_module._session_factory is factory_b
    assert decorator_module._eta_tolerance_seconds == 5


def test_unconfigured_worker_warns_once_instead_of_failing_silently(celery_app, caplog):
    """An unconfigured worker should be loud but harmless: warn, don't raise,
    and don't spam once per task."""

    @celery_app.task(name="setup_demo.unconfigured", bind=True)
    @celery_manager.track()
    def echo(self, value):
        return value

    with caplog.at_level(logging.WARNING, logger="celery_manager.decorator"):
        first = echo.apply(args=["hi"])
        second = echo.apply(args=["hi"])

    assert first.result == "hi"
    assert second.result == "hi"
    warnings = [r for r in caplog.records if "never called" in r.getMessage()]
    assert len(warnings) == 1
