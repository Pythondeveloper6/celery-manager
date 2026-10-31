"""Non-Celery runner adapters record real runner lifecycles as observed runs."""
import asyncio
import threading

import pytest
from sqlalchemy import select

from celery_manager.config import CeleryManagerConfig
from celery_manager.db.models import TaskRun
from celery_manager.observation import RunObserver
from celery_manager.runtime import setup


@pytest.fixture
def observer(tmp_path):
    return RunObserver(CeleryManagerConfig(celery_app=None, db_url=f"sqlite:///{tmp_path / 'cm.db'}"))


def runs(observer, name=None):
    with setup(observer.config)() as session:
        query = select(TaskRun).where(TaskRun.source == "external").order_by(TaskRun.id)
        if name:
            query = query.where(TaskRun.task_name == name)
        return [(row.task_id, row.task_name, row.status, row.exception) for row in session.scalars(query)]


# --- plain loops -----------------------------------------------------------


def test_run_every_records_each_run_and_survives_failures(observer):
    from celery_manager.adapters.loop import run_every

    calls = []

    async def job():
        calls.append(1)
        if len(calls) == 2:
            raise ValueError("boom")

    count = asyncio.run(run_every(observer, "loop.job", job, seconds=0.01, max_runs=3))
    assert count == 3
    assert [status for _, _, status, _ in runs(observer)] == ["succeeded", "failed", "succeeded"]
    assert "ValueError: boom" in runs(observer)[1][3]


def test_run_every_runs_sync_jobs_off_the_event_loop(observer):
    from celery_manager.adapters.loop import run_every

    loop_threads = []

    def job():
        loop_threads.append(threading.current_thread() is threading.main_thread())

    asyncio.run(run_every(observer, "loop.sync", job, seconds=0.01, max_runs=2))
    assert loop_threads == [False, False]


def test_run_every_stops_when_the_event_is_set(observer):
    from celery_manager.adapters.loop import run_every

    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(run_every(observer, "loop.stop", lambda: None, seconds=30, stop=stop))
        await asyncio.sleep(0.2)
        stop.set()
        return await asyncio.wait_for(task, 5)

    assert asyncio.run(main()) == 1


def test_cancelling_the_loop_marks_the_running_job_interrupted(observer):
    from celery_manager.adapters.loop import run_every

    async def main():
        started = asyncio.Event()

        async def job():
            started.set()
            await asyncio.sleep(30)

        task = asyncio.create_task(run_every(observer, "loop.cancel", job, seconds=60))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert [status for _, _, status, _ in runs(observer)] == ["interrupted"]


def test_run_every_rejects_a_non_positive_interval(observer):
    from celery_manager.adapters.loop import run_every

    with pytest.raises(ValueError):
        asyncio.run(run_every(observer, "x", lambda: None, seconds=0))


def test_thread_loop_records_runs_until_stopped(observer):
    from celery_manager.adapters.loop import run_every_in_thread

    done = threading.Event()
    calls = []

    def job():
        calls.append(1)
        if len(calls) >= 2:
            done.set()

    thread = run_every_in_thread(observer, "thread.job", job, seconds=0.01)
    assert done.wait(5)
    thread.stop_event.set()
    thread.join(5)
    assert not thread.is_alive()
    assert {status for _, _, status, _ in runs(observer)} == {"succeeded"}


def test_observed_job_decorates_a_loop_body(observer):
    from celery_manager.adapters.loop import observed_job

    @observed_job(observer, "loop.decorated")
    def body():
        return 1

    assert body() == 1 and body() == 1
    assert len(runs(observer, "loop.decorated")) == 2


# --- arq ---------------------------------------------------------------------


def test_arq_wrapper_records_each_try_and_reraises(observer):
    from celery_manager.adapters.arq import observed

    observe = observed(observer)

    @observe
    async def send_invoice(ctx, invoice_id):
        if ctx["job_try"] == 1:
            raise RuntimeError("smtp down")
        return {"sent": invoice_id}

    with pytest.raises(RuntimeError):
        asyncio.run(send_invoice({"job_id": "j1", "job_try": 1}, 7))
    assert asyncio.run(send_invoice({"job_id": "j1", "job_try": 2}, 7)) == {"sent": 7}
    name = f"{__name__}.test_arq_wrapper_records_each_try_and_reraises.<locals>.send_invoice"
    assert [(task_id, status) for task_id, _, status, _ in runs(observer, name)] == [
        ("external:j1:1", "failed"), ("external:j1:2", "succeeded")]


def test_arq_wrapper_accepts_an_explicit_name(observer):
    from celery_manager.adapters.arq import observed

    @observed(observer, name="billing.invoice")
    async def job(ctx):
        return None

    asyncio.run(job({"job_id": "j2", "job_try": 1}))
    assert runs(observer, "billing.invoice")[0][2] == "succeeded"


# --- Dramatiq ----------------------------------------------------------------


def test_dramatiq_middleware_records_queued_success_failure_and_retries(observer):
    dramatiq = pytest.importorskip("dramatiq")
    from dramatiq.brokers.stub import StubBroker

    from celery_manager.adapters.dramatiq import CeleryManagerMiddleware

    broker = StubBroker()
    broker.add_middleware(CeleryManagerMiddleware(observer))
    dramatiq.set_broker(broker)
    attempts = []

    @dramatiq.actor(broker=broker, max_retries=1, min_backoff=1, max_backoff=1)
    def flaky(x):
        attempts.append(x)
        if len(attempts) == 1:
            raise ValueError("first try fails")
        return x * 2

    @dramatiq.actor(broker=broker)
    def fine():
        return "ok"

    message = flaky.send(21)
    fine.send()
    worker = dramatiq.Worker(broker, worker_timeout=50)
    worker.start()
    try:
        broker.join(flaky.queue_name, timeout=10_000)
        worker.join()
    finally:
        worker.stop()

    flaky_runs = runs(observer, "flaky")
    assert [(task_id, status) for task_id, _, status, _ in flaky_runs] == [
        (f"external:{message.message_id}:0", "failed"), (f"external:{message.message_id}:1", "succeeded")]
    assert "first try fails" in flaky_runs[0][3]
    assert [status for _, _, status, _ in runs(observer, "fine")] == ["succeeded"]


def test_dramatiq_skipped_message_is_recorded_as_skipped(observer):
    pytest.importorskip("dramatiq")
    from types import SimpleNamespace

    from celery_manager.adapters.dramatiq import CeleryManagerMiddleware

    middleware = CeleryManagerMiddleware(observer, record_enqueue=False)
    message = SimpleNamespace(message_id="m1", actor_name="aged", queue_name="default", options={})
    middleware.after_skip_message(None, message)
    assert runs(observer, "aged")[0][2] == "skipped"


# --- Taskiq ------------------------------------------------------------------


def test_taskiq_middleware_records_success_and_failure(observer):
    pytest.importorskip("taskiq")
    from taskiq import InMemoryBroker

    from celery_manager.adapters.taskiq import CeleryManagerMiddleware

    broker = InMemoryBroker().with_middlewares(CeleryManagerMiddleware(observer))

    @broker.task(task_name="maths.double")
    async def double(x: int) -> int:
        return x * 2

    @broker.task(task_name="maths.broken")
    async def broken() -> None:
        raise ValueError("nope")

    async def main():
        await broker.startup()
        ok = await double.kiq(4)
        assert (await ok.wait_result(timeout=5)).return_value == 8
        bad = await broken.kiq()
        assert (await bad.wait_result(timeout=5)).is_err
        await broker.shutdown()
        return ok.task_id, bad.task_id

    ok_id, bad_id = asyncio.run(main())
    assert runs(observer, "maths.double") == [(f"external:{ok_id}", "maths.double", "succeeded", None)]
    failed = runs(observer, "maths.broken")[0]
    assert failed[0] == f"external:{bad_id}" and failed[2] == "failed" and "nope" in failed[3]
