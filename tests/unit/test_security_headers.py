"""Responses carry a script-restricting CSP and basic hardening headers."""
import base64
import hashlib
import re
from pathlib import Path

from celery import Celery
from fastapi.testclient import TestClient

import celery_manager
from celery_manager.api.app import create_app
from celery_manager.config import CeleryManagerConfig

INDEX = Path(celery_manager.__file__).parent / "dashboard" / "static" / "index.html"


def _client(**options):
    app, _ = create_app(CeleryManagerConfig(celery_app=Celery("h"), db_url="sqlite:///:memory:",
                                            secret_key="test-secret", **options))
    return TestClient(app, base_url="https://testserver")


def _policy(response):
    return dict(part.strip().split(" ", 1) for part in response.headers["content-security-policy"].split(";"))


def test_dashboard_page_allows_only_its_own_and_hashed_inline_scripts():
    response = _client().get("/")
    assert response.status_code == 200
    policy = _policy(response)
    inline = re.findall(r"<script>(.*?)</script>", INDEX.read_text(), re.S)
    hashes = {"'sha256-" + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() + "'" for s in inline}
    assert inline and hashes <= set(policy["script-src"].split())
    assert "'unsafe-inline'" not in policy["script-src"] and "'unsafe-eval'" not in policy["script-src"]
    assert policy["object-src"] == "'none'" and policy["base-uri"] == "'self'"
    assert policy["frame-ancestors"] == "'none'"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "same-origin"


def test_api_responses_carry_the_same_headers():
    response = _client().get("/api/auth/options")
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_configured_frame_ancestors_are_kept():
    response = _client(frame_ancestors=("https://host.example",)).get("/")
    assert _policy(response)["frame-ancestors"] == "https://host.example"
    assert "x-frame-options" not in response.headers
