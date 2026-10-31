"""`doctor` reviews Celery settings and can print a fix-it checklist."""
from celery import Celery

from celery_manager.celery_checklist import render_checklist, review
from celery_manager.config import CeleryManagerConfig
from celery_manager.doctor import doctor


def by_name(app):
    return {finding.name: finding for finding in review(app)}


def test_celery_defaults_get_concrete_fixes():
    findings = by_name(Celery("defaults", broker="redis://localhost:6379/0"))
    for name in ("acks_late", "time_limits", "task_events", "track_started", "single_beat", "visibility_timeout_eta"):
        assert not findings[name].ok and findings[name].fix, name
    assert findings["result_expires"].ok  # Celery defaults to one day
    assert "reject_on_worker_lost" not in findings  # only relevant with acks_late


def test_hardened_configuration_passes():
    app = Celery("hardened", broker="redis://localhost:6379/0")
    app.conf.update(task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1,
                    broker_transport_options={"visibility_timeout": 172800}, task_time_limit=3600,
                    task_soft_time_limit=3300, worker_send_task_events=True, task_send_sent_event=True,
                    task_track_started=True, broker_connection_retry_on_startup=True)
    open_items = {name for name, finding in by_name(app).items() if not finding.ok}
    assert open_items == {"single_beat"}


def test_visibility_timeout_shorter_than_the_time_limit_is_flagged():
    app = Celery("vt", broker="redis://localhost:6379/0")
    app.conf.update(broker_transport_options={"visibility_timeout": 600}, task_time_limit=3600)
    finding = by_name(app)["visibility_timeout"]
    assert not finding.ok and "runs twice" in finding.fix


def test_prefetch_matters_only_with_acks_late():
    app = Celery("prefetch", broker="amqp://")
    assert by_name(app)["prefetch_multiplier"].ok
    app.conf.task_acks_late = True
    assert not by_name(app)["prefetch_multiplier"].ok


def test_amqp_brokers_skip_visibility_checks():
    assert "visibility_timeout" not in by_name(Celery("amqp", broker="amqp://guest@localhost//"))


def test_doctor_reports_findings_as_info_never_failures(tmp_path):
    config = CeleryManagerConfig(celery_app=Celery("doc", broker="redis://localhost:6379/0"),
                                 db_url=f"sqlite:///{tmp_path / 'cm.db'}", secret_key="private-test-key")
    report = doctor(config)
    celery_checks = [check for check in report["checks"] if check["name"].startswith("celery_")]
    assert celery_checks and {check["state"] for check in celery_checks} <= {"pass", "info"}
    assert all("fix" in check for check in celery_checks if check["state"] == "info")


def test_rendered_checklist_ticks_passes_and_lists_fixes():
    report = {"environment": "prod", "checked_at": "now", "checks": [
        {"name": "storage", "state": "pass", "detail": "ok"},
        {"name": "celery_acks_late", "state": "info", "detail": "task_acks_late=False", "fix": "Set it."},
    ]}
    text = render_checklist(report)
    assert "- [x] **storage**: ok" in text
    assert "- [ ] **celery_acks_late**: task_acks_late=False\n  - Fix: Set it." in text
    assert text.startswith("# celery-manager production checklist (prod)")


def test_cli_fix_suggestions_prints_markdown(tmp_path, capsys, monkeypatch):
    import pytest

    from celery_manager import cli

    app = Celery("cli-doc", broker="memory://")
    monkeypatch.setattr(cli, "load_celery_app", lambda path: app)
    with pytest.raises(SystemExit):
        cli.main(["doctor", "--celery-app", "x:app", "--db-url", f"sqlite:///{tmp_path / 'cm.db'}", "--fix-suggestions"])
    out = capsys.readouterr().out
    assert out.startswith("# celery-manager production checklist") and "Fix:" in out
