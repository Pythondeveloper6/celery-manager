from datetime import datetime, timedelta, timezone

from celery import Celery
from fastapi.testclient import TestClient

from celery_manager import CeleryManagerConfig
from celery_manager.api.app import create_app
from celery_manager.auth.external import ExternalUser
from celery_manager.control.worker_history import list_heartbeats, prune_heartbeats
from celery_manager.db.models import WorkerHeartbeatSample, WorkerState
from celery_manager.db.session import get_session_factory, init_db
from celery_manager.events.workers import record_worker_event


def test_minute_bucket_dedup_out_of_order_and_offline_ties():
    config=CeleryManagerConfig(celery_app=Celery("history"),db_url="sqlite:///:memory:",secret_key="s")
    engine=init_db(config);sessions=get_session_factory(engine)
    now=datetime.now(timezone.utc).replace(second=10,microsecond=0)
    def event(kind,stamp,active=1):
        return {"type":kind,"hostname":"worker","timestamp":stamp.timestamp(),"active":active,"processed":10}
    with sessions() as session:
        record_worker_event(session,event("worker-heartbeat",now));session.commit()
        record_worker_event(session,event("worker-heartbeat",now+timedelta(seconds=10),2));session.commit()
        record_worker_event(session,event("worker-heartbeat",now-timedelta(minutes=1),9));session.commit()
        assert session.query(WorkerHeartbeatSample).count()==1
        assert session.query(WorkerHeartbeatSample).one().active==2
        record_worker_event(session,event("worker-offline",now+timedelta(seconds=10)));session.commit()
        record_worker_event(session,event("worker-heartbeat",now+timedelta(seconds=10)));session.commit()
        assert session.get(WorkerState,"worker").state=="offline"
        record_worker_event(session,event("worker-heartbeat",now+timedelta(minutes=1)));session.commit()
        assert session.query(WorkerHeartbeatSample).count()==2
        rows,total=list_heartbeats(session,"worker",limit=1)
        assert total==2 and rows[0].state=="online"
    engine.dispose()


def test_history_can_be_disabled_without_losing_last_seen_and_retention():
    config=CeleryManagerConfig(celery_app=None,db_url="sqlite:///:memory:")
    engine=init_db(config);sessions=get_session_factory(engine)
    now=datetime.now(timezone.utc)
    with sessions() as session:
        record_worker_event(session,{"type":"worker-heartbeat","hostname":"worker","timestamp":now.timestamp()},retention_days=0)
        session.commit()
        assert session.get(WorkerState,"worker") is not None
        assert session.query(WorkerHeartbeatSample).count()==0
        session.add(WorkerHeartbeatSample(hostname="old",bucket_at=now-timedelta(days=9),observed_at=now-timedelta(days=9),state="online"));session.commit()
        assert prune_heartbeats(session,7,now=now,dry_run=True)==1
        assert session.query(WorkerHeartbeatSample).count()==1
        assert prune_heartbeats(session,7,now=now)==1
        assert session.query(WorkerHeartbeatSample).count()==0
    engine.dispose()


def test_history_route_range_paging_and_auth():
    config=CeleryManagerConfig(celery_app=Celery("history-api"),db_url="sqlite:///:memory:",secret_key="s",
                              authenticate_request=lambda r: ExternalUser("viewer","viewer") if r.headers.get("x-user") else None)
    app,sessions=create_app(config);client=TestClient(app,base_url="https://testserver")
    assert client.get("/api/workers/worker/heartbeats").status_code==401
    now=datetime.now(timezone.utc)
    with sessions() as session:
        for minute in range(3):
            record_worker_event(session,{"type":"worker-heartbeat","hostname":"worker","timestamp":(now+timedelta(minutes=minute)).timestamp()});session.commit()
    response=client.get("/api/workers/worker/heartbeats?limit=1&offset=1",headers={"x-user":"yes"})
    assert response.status_code==200 and response.json()["total"]==3 and len(response.json()["items"])==1
    assert response.json()["items"][0]["observed_at"].endswith("+00:00")
    assert client.get("/api/workers/worker/heartbeats?limit=1001",headers={"x-user":"yes"}).status_code==422
    assert client.get("/api/workers/worker/heartbeats?since=2030-01-01&until=2020-01-01",headers={"x-user":"yes"}).status_code==422
