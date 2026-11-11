"""Test tiers.

Every test under ``tests/integration/`` is marked ``integration`` every test under
``tests/e2e/`` ``e2e`` (real processes; ``pytest tests/e2e -m e2e``) and every
test elsewhere ``unit``, so the fast tier is one flag away::

    pytest -m unit            # no services, a few minutes serially
    pytest -m integration     # skips what its CM_* service variables don't enable

Tests that need a live service also skip themselves when the matching
environment variable is unset; see docs/contributing.md for the list.
"""
from pathlib import Path

import pytest

_INTEGRATION = Path(__file__).parent / "integration"
_E2E = Path(__file__).parent / "e2e"


def pytest_collection_modifyitems(config, items):
    for item in items:
        path = Path(str(item.fspath))
        if _INTEGRATION in path.parents:
            item.add_marker(pytest.mark.integration)
        elif _E2E in path.parents:
            item.add_marker(pytest.mark.e2e)
        else:
            item.add_marker(pytest.mark.unit)


_DECORATOR_GLOBALS = (
    "_session_factory", "_eta_tolerance_seconds", "_warned_unconfigured",
    "_warned_legacy_match", "_beat_stamps", "_config",
)


@pytest.fixture(autouse=True)
def _restore_decorator_globals():
    """`configure()` and the beat-stamp detector mutate module-global state in
    `celery_manager.decorator`; restore it so tests cannot leak into each other."""
    from celery_manager import decorator

    saved = {name: getattr(decorator, name) for name in _DECORATOR_GLOBALS}
    yield
    for name, value in saved.items():
        setattr(decorator, name, value)
