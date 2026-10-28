"""Release eligibility must survive CI-fix commits without another version bump."""
import runpy
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

pytestmark = pytest.mark.skipif(sys.version_info < (3, 11), reason="Publishing helper runs on Python 3.12")

script = Path(__file__).resolve().parents[2] / ".github/scripts/publish_status.py"


def helpers():
    return runpy.run_path(str(script))


def test_published_version_is_skipped():
    ns = helpers()
    with patch("urllib.request.urlopen") as open_url:
        # runpy binds the function's globals; patch that binding explicitly.
        ns["needs_publish"].__globals__["urlopen"] = open_url
        open_url.return_value.__enter__.return_value.status = 200
        assert ns["needs_publish"]("celery-manager", "0.2.0a1") is False


@pytest.mark.parametrize("code, expected", [(404, True), (403, None), (503, None)])
def test_only_not_found_is_publishable(code, expected):
    ns = helpers()
    ns["needs_publish"].__globals__["urlopen"] = MagicMock(side_effect=HTTPError("url", code, "error", {}, None))
    if expected is None:
        with pytest.raises(HTTPError):
            ns["needs_publish"]("celery-manager", "0.2.0a1")
    else:
        assert ns["needs_publish"]("celery-manager", "0.2.0a1") is True


def test_network_failure_does_not_enable_publishing():
    ns = helpers()
    ns["needs_publish"].__globals__["urlopen"] = MagicMock(side_effect=URLError("offline"))
    with pytest.raises(URLError):
        ns["needs_publish"]("celery-manager", "0.2.0a1")


def test_unchanged_unpublished_version_can_retry(tmp_path, monkeypatch):
    ns = helpers()
    globals_ = ns["main"].__globals__
    globals_["needs_publish"] = MagicMock(return_value=True)
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    project = '[project]\nname="celery-manager"\nversion="0.2.0a1"\n'
    with patch.object(Path, "read_text", return_value=project), patch("subprocess.check_output", return_value=project):
        ns["main"]()
    assert "publish=true" in output.read_text()
