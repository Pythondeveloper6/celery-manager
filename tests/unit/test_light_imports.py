"""Monitoring-only processes must not import Celery, FastAPI or bcrypt.

Each check runs in a fresh interpreter: this test process has long since
imported everything.
"""
import subprocess
import sys
from pathlib import Path

import celery_manager

HEAVY = ("celery", "kombu", "fastapi", "starlette", "bcrypt", "itsdangerous", "pydantic")
SRC = str(Path(celery_manager.__file__).parents[1])


def _run(code, tmp_path):
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False,
                            cwd=tmp_path, env={"PYTHONPATH": SRC, "PATH": "/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_observing_a_host_job_imports_no_framework(tmp_path):
    out = _run(f"""
import sys
from celery_manager.config import CeleryManagerConfig
from celery_manager.observation import RunObserver
observer = RunObserver(CeleryManagerConfig(celery_app=None, db_url="sqlite:///{tmp_path / 'cm.db'}"))
with observer.observe("nightly.export"):
    pass
print(observer.health()["writes_succeeded"])
print(",".join(sorted(m for m in {HEAVY!r} if m in sys.modules)))
""", tmp_path)
    succeeded, loaded = out.split("\n")[:2]
    assert int(succeeded) >= 2
    assert loaded == ""


def test_package_root_resolves_names_on_first_use(tmp_path):
    out = _run("""
import sys
import celery_manager
before = "celery" in sys.modules
track = celery_manager.track
print(before, "celery" in sys.modules, callable(track), "track" in dir(celery_manager))
""", tmp_path)
    assert out.split() == ["False", "True", "True", "True"]


def test_unknown_root_attribute_still_raises():
    import pytest

    with pytest.raises(AttributeError):
        celery_manager.not_a_real_name  # noqa: B018
