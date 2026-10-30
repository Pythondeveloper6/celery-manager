"""Host-neutral contracts shared by SQLite and disposable server gates."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from celery_manager import RunObserver
from celery_manager.control.health import aware
from celery_manager.db.health_data import HealthObservation
from celery_manager.db.models import OperationAnnotation, TaskRun
from celery_manager.health_probes import (
    ProbeDefinition,
    ProbeResult,
    _persist_observation,
    collect_probes,
    probe_status,
)
from celery_manager.runtime import setup
from celery_manager.scheduler_health import (
    ExpectedCommand,
    register_scheduler,
    scheduler_heartbeat,
    scheduler_status,
)


def exercise_native_operability(config):
    factory = setup(config)
    observer = RunObserver(config, fail_open=False)
    for tenant in ("a", "b"):
        with observer.observe("host.command", tenant_id=tenant, operation_key="shared-business-key", correlation_id="request"):
            pass
    assert len(observer.duplicates("shared-business-key", tenant_id="a")) == 1
    # Default MySQL/SQL Server collations can equate these distinct identities.
    # The same strict gate exercises Unicode storage and byte-exact filtering.
    tenants = ("Case", "case", "cáse", "ca\u0301se", "case ", "شركة")
    for tenant in tenants:
        with observer.observe("host.identity", tenant_id=tenant, operation_key="same"):
            pass
    for tenant in tenants:
        rows = observer.duplicates("same", tenant_id=tenant)
        assert len(rows) == 1 and rows[0]["tenant_id"] == tenant
    keys = ("Operation", "operation", "opératio", "operation ", "عملية")
    for key in keys:
        with observer.observe("host.identity", tenant_id="a", operation_key=key):
            pass
    for key in keys:
        rows = observer.duplicates(key, tenant_id="a")
        assert len(rows) == 1 and rows[0]["operation_key"] == key
    def collect(_):
        from copy import copy
        local = copy(config)
        local.health_probes = (ProbeDefinition("native-outbox", lambda: ProbeResult("healthy", {"depth": 1})),)
        collect_probes(local, factory)
        with factory() as session:
            register_scheduler(session, "native-scheduler", [ExpectedCommand("host.command", "* * * * *")])
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(collect, range(12)))
    with factory() as session:
        assert probe_status(session)[0]["state"] == "healthy"
        row = session.get(HealthObservation, "native-outbox")
        latest = aware(row.observed_at)
        _persist_observation(session, "native-outbox", ProbeResult("degraded", {"depth": 999}),
                             None, latest - timedelta(seconds=1))
        session.commit()
        session.expire_all()
        assert probe_status(session)[0]["state"] == "healthy"
        assert aware(session.get(HealthObservation, "native-outbox").observed_at) == latest
        scheduler_heartbeat(session, "native-scheduler")
        assert len(scheduler_status(session)) == 1
        assert session.query(TaskRun).filter_by(source="external").count() == 2 + len(tenants) + len(keys)
        at = datetime.now(timezone.utc)
        session.add(OperationAnnotation(kind="deployment", environment="test", label="Native contract إصدار 🚀", revision="إصدار-1", note="صيانة\n🚀", actor="test", starts_at=at, created_at=at))
        session.commit()
        annotation = session.query(OperationAnnotation).one()
        assert annotation.label == "Native contract إصدار 🚀"
        assert annotation.revision == "إصدار-1" and annotation.note == "صيانة\n🚀"
