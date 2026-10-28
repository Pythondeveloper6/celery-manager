"""Real adapter contracts. CI installs the optional scheduler extras."""
import importlib
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from celery import Celery
from celery.schedules import crontab, schedule

pytest.importorskip("redbeat")
fakeredis = pytest.importorskip("fakeredis")
pytest.importorskip("django_celery_beat")

from celery_manager.schedulers import (
    DjangoCeleryBeatBackend,
    RedBeatBackend,
    ScheduleEntry,
)


@pytest.fixture(params=["redbeat", "django"])
def adapter(request):
    app = Celery("adapter-contract", broker="memory://")
    app.conf.timezone = "UTC"
    if request.param == "redbeat":
        redis = fakeredis.FakeRedis()
        with patch("redbeat.schedulers.get_redis", return_value=redis):
            yield RedBeatBackend(app), app
    else:
        import django
        from django.conf import settings
        if not settings.configured:
            settings.configure(INSTALLED_APPS=["django_celery_beat"], USE_TZ=True,
                               DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}})
            django.setup()
        from django.core.management import call_command
        from django_celery_beat.models import PeriodicTask
        call_command("migrate", verbosity=0)
        PeriodicTask.objects.all().delete()
        yield DjangoCeleryBeatBackend(app), app


@pytest.mark.parametrize("timing", [crontab(minute="0", hour="6"), schedule(timedelta(minutes=15))])
def test_adapter_roundtrip_contract(adapter, timing):
    backend, _ = adapter
    entry = ScheduleEntry("contract", "contract.task", timing, [1], {"x": 2}, {"queue": "slow"}, source=backend.name)
    backend.create(entry)
    with pytest.raises(FileExistsError):
        backend.create(entry)
    loaded = backend.get("contract")
    assert loaded.args == [1] and loaded.kwargs == {"x": 2} and loaded.options["queue"] == "slow"
    backend.update("contract", schedule=schedule(30), enabled=False)
    entries = list(backend.list())
    assert len(entries) == 1 and entries[0].enabled is False
    backend.delete("contract")
    assert backend.get("contract") is None


def test_shipped_scheduler_stamps_and_advances_once(adapter):
    backend, app = adapter
    @app.task(name="contract.task", shared=False)
    def task():
        return 1
    backend.create(ScheduleEntry("stamped", "contract.task", schedule(60), source=backend.name))
    if backend.name == "redbeat":
        entry = backend.Entry.from_key(backend.conf.key_prefix + "stamped", app=app)
    else:
        entry = backend.Entry(backend.Model.objects.get(name="stamped"), app=app)
    path = backend.beat_scheduler_class()
    module, name = path.split(":")
    scheduler = getattr(importlib.import_module(module), name)(app=app, lazy=True)
    with patch.object(task, "apply_async", return_value=MagicMock(id="sent")) as send:
        scheduler.apply_async(entry)
    headers = send.call_args.kwargs["headers"]
    assert headers["cm_schedule"] == "stamped" and headers["cm_occurrence"]
    if backend.name == "redbeat":
        stored = backend.Entry.from_key(backend.conf.key_prefix + "stamped", app=app)
        assert stored.total_run_count == 1
    else:
        scheduler.sync()
        assert backend.Model.objects.get(name="stamped").total_run_count == 1
