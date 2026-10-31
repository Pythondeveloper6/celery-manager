"""Delivering one alert message to one channel: Slack, Teams, PagerDuty, a
webhook, or email.

Standard library only (`urllib`, `smtplib`): celery-manager runs inside
other people's processes and should not pull an HTTP client into them for
one POST a minute at most.

Every sender takes `(channel config, message, CeleryManagerConfig)` and
either returns or raises `DeliveryError` with a sentence the dashboard
shows on the channel. Every outbound call has `alert_send_timeout_seconds`
as its time limit.

Webhook payload (`"schema": 1`), POSTed as JSON:

    {
      "schema": 1,
      "event": "alert.firing" | "alert.resolved" | "alert.test",
      "summary": "One line for a person",
      "rule": {"id": 3, "name": "...", "kind": "failure_rate", "target": "*"} | null,
      "key": "reports.build",            # the task/queue/worker/schedule
      "details": {...},                  # kind-specific numbers
      "opened_at": "ISO-8601" | null,
      "resolved_at": "ISO-8601" | null,
      "url": "https://..." | null,       # needs CeleryManagerConfig.dashboard_url
      "sent_at": "ISO-8601"
    }

With a secret set, the request carries `X-Celery-Manager-Timestamp` (Unix
seconds) and `X-Celery-Manager-Signature: sha256=<hex>`, the HMAC-SHA256 of
`"<timestamp>." + body` keyed with the secret. Signing the timestamp lets a
receiver reject replays of an old, validly signed request.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage

from celery_manager.config import CeleryManagerConfig

SIGNATURE_HEADER = "X-Celery-Manager-Signature"
TIMESTAMP_HEADER = "X-Celery-Manager-Timestamp"
USER_AGENT = "celery-manager-alerts"
# How much of an error response body to keep in a channel's last error.
_ERROR_BODY_LIMIT = 200


class DeliveryError(Exception):
    """The channel did not accept the message. The text is shown as is."""


@dataclass
class AlertMessage:
    event: str  # firing | resolved | test
    summary: str
    rule: dict | None = None
    key: str | None = None
    details: dict = field(default_factory=dict)
    opened_at: str | None = None
    resolved_at: str | None = None
    url: str | None = None
    sent_at: str | None = None

    def payload(self) -> dict:
        return {
            "schema": 1,
            "event": f"alert.{self.event}",
            "summary": self.summary,
            "rule": self.rule,
            "key": self.key,
            "details": self.details,
            "opened_at": self.opened_at,
            "resolved_at": self.resolved_at,
            "url": self.url,
            "sent_at": self.sent_at,
        }

    def subject(self) -> str:
        prefix = {"firing": "[ALERT]", "resolved": "[RESOLVED]", "test": "[TEST]"}.get(self.event, "[ALERT]")
        return f"{prefix} {self.summary}"

    def text(self, include_url: bool = True) -> str:
        lines = [self.subject()]
        if self.rule:
            lines.append(f"Rule: {self.rule['name']} ({self.rule['kind']}, target {self.rule['target']})")
        for key, value in self.details.items():
            lines.append(f"{key.replace('_', ' ')}: {value}")
        if self.opened_at:
            lines.append(f"Since: {self.opened_at}")
        if self.resolved_at:
            lines.append(f"Resolved: {self.resolved_at}")
        if self.url and include_url:
            lines.append(self.url)
        return "\n".join(lines)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects. The admin vetted one URL; a 3xx from it could
    point the server at any other address, including internal ones."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"redirect to {newurl} refused", headers, fp)


_opener = urllib.request.build_opener(_NoRedirect)


def signature(secret: str, timestamp: str, body: bytes) -> str:
    """The `X-Celery-Manager-Signature` value for `body` sent at `timestamp`."""
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def post_json(url: str, body: bytes, headers: dict[str, str], timeout: float) -> None:
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", USER_AGENT)
    for name, value in headers.items():
        request.add_header(name, value)
    try:
        with _opener.open(request, timeout=timeout) as response:
            response.read(_ERROR_BODY_LIMIT)
    except urllib.error.HTTPError as exc:
        try:
            snippet = exc.read(_ERROR_BODY_LIMIT).decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001 - the status is the useful part
            snippet = ""
        detail = f": {snippet}" if snippet else ""
        raise DeliveryError(f"the endpoint answered HTTP {exc.code}{detail}") from None
    except urllib.error.URLError as exc:
        raise DeliveryError(f"could not reach the endpoint: {exc.reason}") from None
    except TimeoutError:
        raise DeliveryError(f"the endpoint did not answer within {timeout:g}s") from None
    except OSError as exc:
        raise DeliveryError(f"could not reach the endpoint: {exc}") from None


def send_slack(channel_config: dict, message: AlertMessage, config: CeleryManagerConfig) -> None:
    # `text` is the notification preview and the fallback; the block shows
    # the full message with the dashboard link clickable.
    mrkdwn = _slack_escape(message.text(include_url=False))
    if message.url:
        mrkdwn += f"\n<{_slack_escape(message.url)}|Open in the dashboard>"
    body = {
        "text": message.subject(),
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": mrkdwn}}],
    }
    post_json(channel_config["url"], json.dumps(body).encode(), {}, config.alert_send_timeout_seconds)


def _slack_escape(text: str) -> str:
    # Slack mrkdwn treats &, < and > as control characters; | splits a link.
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_webhook(channel_config: dict, message: AlertMessage, config: CeleryManagerConfig) -> None:
    body = json.dumps(message.payload(), sort_keys=True).encode()
    headers = {}
    secret = channel_config.get("secret")
    if secret:
        timestamp = str(int(time.time()))
        headers[TIMESTAMP_HEADER] = timestamp
        headers[SIGNATURE_HEADER] = signature(secret, timestamp, body)
    post_json(channel_config["url"], body, headers, config.alert_send_timeout_seconds)


def send_email(channel_config: dict, message: AlertMessage, config: CeleryManagerConfig) -> None:
    if not config.smtp_host:
        raise DeliveryError("email is not set up: set smtp_host on CeleryManagerConfig")
    if not config.alert_email_from:
        raise DeliveryError("email is not set up: set alert_email_from on CeleryManagerConfig")
    email = EmailMessage()
    email["Subject"] = message.subject()
    email["From"] = config.alert_email_from
    email["To"] = ", ".join(channel_config["to"])
    email.set_content(message.text())
    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=config.alert_send_timeout_seconds) as smtp:
            if config.smtp_use_tls:
                smtp.starttls(context=ssl.create_default_context())
            if config.smtp_username:
                smtp.login(config.smtp_username, config.smtp_password or "")
            smtp.send_message(email)
    except smtplib.SMTPAuthenticationError:
        raise DeliveryError("the SMTP server refused the username or password") from None
    except smtplib.SMTPException as exc:
        raise DeliveryError(f"the SMTP server refused the message: {exc}") from None
    except TimeoutError:
        raise DeliveryError(f"the SMTP server did not answer within {config.alert_send_timeout_seconds:g}s") from None
    except OSError as exc:
        raise DeliveryError(f"could not reach the SMTP server: {exc}") from None


def send_teams(channel_config, message, config):
    card = {"type": "AdaptiveCard", "version": "1.2",
            "body": [{"type": "TextBlock", "text": message.subject(), "weight": "Bolder", "wrap": True},
                     {"type": "TextBlock", "text": message.text(include_url=False), "wrap": True}]}
    if message.url:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open dashboard", "url": message.url}]
    payload = {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                                                  "contentUrl": None, "content": card}]}
    post_json(channel_config["url"], json.dumps(payload).encode(), {}, config.alert_send_timeout_seconds)


def pagerduty_event(channel_config: dict, message: AlertMessage, config: CeleryManagerConfig) -> dict:
    """The PagerDuty Events API v2 body for `message`.

    The dedup key is the incident's identity (rule id + key), so a resolve
    closes the PagerDuty incident its trigger opened and a repeated trigger
    for the same open incident does not page twice.
    """
    rule_id = message.rule["id"] if message.rule else "test"
    dedup_key = f"celery-manager:{rule_id}:{message.key or '-'}"[:255]
    body: dict = {
        "routing_key": channel_config["routing_key"],
        "event_action": "resolve" if message.event == "resolved" else "trigger",
        "dedup_key": dedup_key,
    }
    if message.event != "resolved":
        body["payload"] = {
            "summary": message.summary[:1024],
            "source": config.environment_name or "celery-manager",
            "severity": "info" if message.event == "test" else "error",
            "component": message.key,
            "group": message.rule["kind"] if message.rule else None,
            "custom_details": {**message.details, "opened_at": message.opened_at},
        }
        if message.url:
            body["links"] = [{"href": message.url, "text": "Open in celery-manager"}]
    return body


def send_pagerduty(channel_config: dict, message: AlertMessage, config: CeleryManagerConfig) -> None:
    body = json.dumps(pagerduty_event(channel_config, message, config), default=str).encode()
    post_json(channel_config["url"], body, {}, config.alert_send_timeout_seconds)


SENDERS = {"slack": send_slack, "webhook": send_webhook, "email": send_email, "teams": send_teams,
           "pagerduty": send_pagerduty}
