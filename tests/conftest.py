"""Test tiers.

Every test under ``tests/integration/`` is marked ``integration`` and every
test elsewhere ``unit``, so the fast tier is one flag away::

    pytest -m unit            # no services, a few minutes serially
    pytest -m integration     # skips what its CM_* service variables don't enable

Tests that need a live service also skip themselves when the matching
environment variable is unset; see docs/contributing.md for the list.
"""
from pathlib import Path

import pytest

_INTEGRATION = Path(__file__).parent / "integration"


def pytest_collection_modifyitems(config, items):
    for item in items:
        path = Path(str(item.fspath))
        if _INTEGRATION in path.parents:
            item.add_marker(pytest.mark.integration)
        else:
            item.add_marker(pytest.mark.unit)
