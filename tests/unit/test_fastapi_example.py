"""Smoke test: the FastAPI example boots, serves its own routes and the mounted dashboard."""
import os
import subprocess
import sys
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "fastapi_demo"

SCRIPT = """
from fastapi.testclient import TestClient
import main
c = TestClient(main.app)
assert c.get("/").json()["hello"]
assert c.post("/welcome/a@example.com").json()["task_id"]
r = c.get("/celery-manager/", follow_redirects=True)
assert r.status_code == 200, r.status_code
print("example-ok")
"""


def test_fastapi_example_runs(tmp_path):
    env = {
        **os.environ,
        "CELERY_MANAGER_EAGER": "1",
        "CELERY_MANAGER_SECRET_KEY": "x" * 40,
        "CELERY_MANAGER_DB_URL": f"sqlite:///{tmp_path / 'demo.db'}",
    }
    out = subprocess.run(
        [sys.executable, "-c", SCRIPT], cwd=EXAMPLE, env=env, check=False,
        capture_output=True, text=True, timeout=120,
    )
    assert "example-ok" in out.stdout, out.stdout + out.stderr
