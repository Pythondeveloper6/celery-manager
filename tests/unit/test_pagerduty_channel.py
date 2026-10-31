"""PagerDuty Events API v2 channel: validation, masking and event bodies."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from celery_manager.alerts.senders import SENDERS, AlertMessage, pagerduty_event
from celery_manager.control.alerts import (
    PAGERDUTY_EVENTS_URL,
    _validate_channel_config,
    channel_entry,
)

KEY = "R0UT1NGKEY0123456789abcdefABCDEF"
CONFIG = SimpleNamespace(environment_name="production", alert_send_timeout_seconds=5)


def message(event="firing", **overrides):
    values = {"event": event, "summary": "reports.daily failed", "key": "t-1",
              "rule": {"id": 7, "name": "Failures", "kind": "task_failed", "target": "*"},
              "details": {"task_name": "reports.daily"}, "url": "https://ops.example.com/#/tasks/t-1",
              "opened_at": "2026-10-29T12:00:00+00:00"}
    values.update(overrides)
    return AlertMessage(**values)


def test_create_defaults_the_events_url_and_requires_a_key():
    assert _validate_channel_config("pagerduty", {"routing_key": KEY}, None) == {
        "url": PAGERDUTY_EVENTS_URL, "routing_key": KEY}
    with pytest.raises(ValueError, match="integration key"):
        _validate_channel_config("pagerduty", {}, None)
    with pytest.raises(ValueError, match="integration key"):
        _validate_channel_config("pagerduty", {"routing_key": "short"}, None)
    with pytest.raises(ValueError, match="integration key"):
        _validate_channel_config("pagerduty", {"routing_key": KEY[:-1] + "!"}, None)


def test_update_keeps_the_stored_key_and_url_when_omitted():
    stored = {"url": "https://events.eu.pagerduty.com/v2/enqueue", "routing_key": KEY}
    assert _validate_channel_config("pagerduty", {}, stored) == stored
    assert _validate_channel_config("pagerduty", {"routing_key": ""}, stored) == stored


def test_unknown_settings_and_bad_urls_are_refused():
    with pytest.raises(ValueError, match="unknown setting"):
        _validate_channel_config("pagerduty", {"routing_key": KEY, "secret": "x"}, None)
    with pytest.raises(ValueError):
        _validate_channel_config("pagerduty", {"routing_key": KEY, "url": "file:///etc/passwd"}, None)


def test_api_shape_never_returns_the_key():
    channel = SimpleNamespace(id=1, name="On call", kind="pagerduty", created_by="admin", created_at=None,
                              updated_at=None, enabled=True, last_error=None, last_sent_at=None,
                              last_error_at=None, config=json.dumps({"url": PAGERDUTY_EVENTS_URL, "routing_key": KEY}))
    entry = channel_entry(channel)
    assert entry["config"] == {"url": "https://events.pagerduty.com/…", "routing_key_set": True}
    assert KEY not in json.dumps(entry, default=str)


def test_trigger_and_resolve_share_a_dedup_key():
    channel = {"url": PAGERDUTY_EVENTS_URL, "routing_key": KEY}
    trigger = pagerduty_event(channel, message(), CONFIG)
    resolve = pagerduty_event(channel, message("resolved"), CONFIG)
    assert trigger["event_action"] == "trigger" and resolve["event_action"] == "resolve"
    assert trigger["dedup_key"] == resolve["dedup_key"] == "celery-manager:7:t-1"
    assert trigger["payload"]["source"] == "production" and trigger["payload"]["severity"] == "error"
    assert trigger["links"] == [{"href": "https://ops.example.com/#/tasks/t-1", "text": "Open in celery-manager"}]
    assert "payload" not in resolve


def test_test_messages_are_info_severity():
    body = pagerduty_event({"routing_key": KEY}, message("test", rule=None, key=None), CONFIG)
    assert body["payload"]["severity"] == "info" and body["dedup_key"] == "celery-manager:test:-"


def test_sender_posts_the_event_to_the_configured_url():
    with patch("celery_manager.alerts.senders.post_json") as post:
        SENDERS["pagerduty"]({"url": PAGERDUTY_EVENTS_URL, "routing_key": KEY}, message(), CONFIG)
    url, body, headers, timeout = post.call_args.args
    assert url == PAGERDUTY_EVENTS_URL and timeout == 5 and headers == {}
    assert json.loads(body)["routing_key"] == KEY
