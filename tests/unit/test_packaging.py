"""Packaging and release-workflow contracts."""
import re
import sys
from pathlib import Path

import pytest

import celery_manager

ROOT = Path(__file__).resolve().parents[2]

tomllib = pytest.importorskip("tomllib") if sys.version_info >= (3, 11) else None


def test_package_ships_a_pep_561_marker():
    assert (Path(celery_manager.__file__).parent / "py.typed").is_file()


@pytest.mark.skipif(tomllib is None, reason="tomllib needs Python 3.11")
def test_typed_classifier_matches_the_marker():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert "Typing :: Typed" in project["classifiers"]


def test_publish_workflow_only_runs_for_version_tags():
    text = (ROOT / ".github/workflows/publish.yml").read_text()
    trigger = text[text.index("\non:") : text.index("\nconcurrency:")]
    assert re.search(r"tags:\s*\[\"v\*\"\]", trigger)
    for other in ("branches", "workflow_run", "pull_request", "schedule", "workflow_dispatch"):
        assert other not in trigger


def test_publish_steps_request_attestations():
    text = (ROOT / ".github/workflows/publish.yml").read_text()
    publishes = text.count("pypa/gh-action-pypi-publish@")
    assert publishes == 2
    assert text.count("attestations: true") == publishes


def test_workflow_actions_are_pinned_to_commits_and_checkouts_drop_credentials():
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = workflow.read_text()
        for ref in re.findall(r"uses: (\S+)", text):
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), f"{workflow.name}: {ref}"
        assert text.count("actions/checkout@") == text.count("persist-credentials: false"), workflow.name
