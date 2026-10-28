"""Seed celery-manager with demo users, alert channels and rules, a
maintenance window and retention rules. Safe to run again: anything that
already exists is left alone."""

from demo.cm import config
from django.core.management.base import BaseCommand

from celery_manager.auth.users import create_user, user_exists
from celery_manager.control.alerts import (
    create_channel,
    create_rule,
    list_channels,
    list_rules,
)
from celery_manager.control.maintenance import create_window, list_windows
from celery_manager.control.retention import update_retention
from celery_manager.runtime import setup

ACTOR = "demo_setup"
# Demo-only credentials. Never reuse these anywhere real.
USERS = [("admin", "admin-demo", "admin"), ("operator", "operator-demo", "operator"), ("viewer", "viewer-demo", "viewer")]
WEBHOOK_SECRET = "demo-webhook-secret"  # alert-sink verifies signatures with the same value

CHANNELS = [
    ("Alert sink (webhook)", "webhook", {"url": "http://alert-sink:9000/hook", "secret": WEBHOOK_SECRET}),
    ("Ops email (Mailpit)", "email", {"to": ["ops@example.com"]}),
]

# (name, kind, target, params). Windows and quiet periods are short so alerts
# show up and clear within minutes.
RULES = [
    ("charge_card failures", "failure_rate", "tasks_app.tasks.charge_card",
     {"rate": 0.3, "min_runs": 4, "window_minutes": 15}),
    ("Worker offline", "worker_offline", "*", {"minutes": 2}),
    ("Heartbeat missed", "missed_run", "heartbeat-every-minute", {"window_minutes": 15}),
    ("Reports queue backlog", "queue_backlog", "reports", {"depth": 20}),
]


class Command(BaseCommand):
    help = "Create the demo dashboard users, alert channels and rules, maintenance window and retention rules."

    def handle(self, *args, **options):
        session_factory = setup(config)
        with session_factory() as session:
            self._users(session)
            ids = self._channels(session)
            self._rules(session, ids)
            self._window(session)
            update_retention(session, {"default_days": 30, "failed_days": 90}, ACTOR)
            self.stdout.write("retention: keep runs 30 days, failed runs 90")
        self.stdout.write(self.style.SUCCESS("demo_setup done"))

    def _users(self, session):
        self.stdout.write("dashboard users (http://localhost:8001):")
        for name, password, role in USERS:
            if not user_exists(session, name):
                create_user(session, name, password, role=role)
            self.stdout.write(f"  {name} / {password}  ({role})")

    def _channels(self, session) -> list[int]:
        existing = {c["name"]: c["id"] for c in list_channels(session)}
        for name, kind, cfg in CHANNELS:
            if name not in existing:
                existing[name] = create_channel(session, ACTOR, name, kind, cfg)["id"]
                self.stdout.write(f"channel created: {name}")
        return [existing[name] for name, _, _ in CHANNELS]

    def _rules(self, session, channel_ids):
        existing = {r["name"] for r in list_rules(session)}
        for name, kind, target, params in RULES:
            if name in existing:
                continue
            create_rule(session, ACTOR, {
                "name": name, "kind": kind, "target": target, "params": params,
                "channel_ids": channel_ids, "quiet_period_minutes": 5,
            })
            self.stdout.write(f"rule created: {name}")

    def _window(self, session):
        name = "Nightly maintenance"
        if any(w.name == name for w in list_windows(session)):
            return
        create_window(session, {
            "name": name, "days_of_week": [0, 1, 2, 3, 4, 5, 6], "start_time": "03:00",
            "duration_minutes": 30, "timezone": "UTC", "applies_to": ["heartbeat-every-minute"],
        }, ACTOR)
        self.stdout.write(f"maintenance window created: {name}")
