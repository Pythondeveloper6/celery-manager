"""Stability contracts from docs/stability.md.

Changing anything pinned here is a compatibility decision: update the policy
page and the CHANGELOG in the same change, never just this file.
"""
import hashlib
import subprocess
import sys
from pathlib import Path

import celery_manager
from celery_manager.cli import build_parser

VERSIONS = Path(celery_manager.__file__).parent / "db" / "migrations" / "versions"

# Revisions that shipped in a release. Never edit one: add a new revision.
# Line endings are normalized so a Windows checkout hashes the same.
SHIPPED_REVISIONS = {
    "0001_baseline.py": "7f711a2e2065e9ed794e8b09c4132fde1afea54a8ca822153ea2dade7795bc67",
    "0002_schedule_pause.py": "412df1747ee237d0cd9e5d1a6f1e633df4cfe7b8f2c2ef2b56dd11bcf9816826",
    "0003_workflow_lineage.py": "6b4589c6bc5a77d9d15db1e1b165a87fc28721a6eaa5ccc5e4e1e67affc72e9b",
    "0004_schedule_paused_until.py": "68e5cefc040dc1af0daa98de1d72324b4b4da1c321fcd8bbf4de7685a8c4e837",
    "0005_task_runs_updated_at.py": "4e9fd77e6640da6f925710532abd694c920b9ab268d35e48e06f111dd949baa9",
    "0006_task_runs_occurrence.py": "4b6154de0d04bfdff41ad05cef610b8080ec0163ec5823f7075483b965d91f58",
    "0007_user_roles.py": "133c5ba96c6c36cca358227e17ff8508d3d06fbe14b0c94b730ceb2a9c9aaa7c",
    "0008_schedule_override.py": "a0d723ef9bafc54737ab644e432309f6b0715d12cf9e7c8f2b84739f99247b1b",
    "0009_alerts.py": "0b035e827d9a9568a19dd35fbe935fc8b39d154a3c6b7b8364bf3de759c11d2a",
    "0010_maintenance_windows.py": "b8e4d57a19762262916899d8619b96acdb5116e5937eba985fba7594bdd88948",
    "0011_queue_wait_and_depth_history.py": "3ed9bdb941980cb61e7fe6dd974f3eaa1d9974c6dd6cf08f21f0b0bacf0c0bba",
    "0012_production_foundations.py": "a9c95097018b491cce573d366634e0349b7109dada3599c21735593dd0ee8a49",
    "0013_utc_defaults.py": "dc8b63afd2e7a028f607ce460efd36474a95e379d0e45809f86bd5e427c0f7b0",
    "0014_worker_states.py": "65d38ea64f61eff83c2fe6aa10685b4809c78321d7f81e7bfda4ce6c6a5f7be8",
    "0015_failure_resolution.py": "c4f54fbbb234d3308cce3526b7ac9af9c637d22d65bb62221af4ae878f3d0c31",
    "0016_auth_state.py": "abed32412ad5028150a3a13c8289c6b19c5b9f6dffb7641132dc66c0b793c493",
    "0017_worker_heartbeat_history.py": "218c099694afc6c30937084036ac3b2d290a2b5e60e04c945750e85b928bcd5b",
    "0018_run_observation.py": "733f0947099ee4ff3229e341f11175049b77b052ed24fc6065b54ddfb0386e00",
    "0019_health_data.py": "5ea2d39965de1762fcb60daa8fc317ea3f4f2654bf2c425bafd692c3ccd5a629",
    "0020_operation_annotations.py": "3b8bd42f8e4c5548dd75d8688d5c620f4fede1ffd71932f8bf40a9bd932b55b1",
}

PUBLIC_NAMES = {
    "CeleryManagerConfig", "RunObserver", "__version__", "control", "current_run",
    "install_publish_hook", "report_progress", "setup", "task_url", "track",
}

CLI_COMMANDS = {
    "events", "alerts", "migrate", "create-user", "set-role", "list-users",
    "prune", "import-pauses", "doctor",
}


def test_shipped_migration_revisions_are_never_edited():
    for name, digest in SHIPPED_REVISIONS.items():
        text = (VERSIONS / name).read_bytes().replace(b"\r", b"")
        assert hashlib.sha256(text).hexdigest() == digest, f"{name} shipped in a release; add a new revision instead"


def test_package_root_keeps_its_public_names():
    assert PUBLIC_NAMES <= set(celery_manager.__all__)
    for name in PUBLIC_NAMES:
        assert hasattr(celery_manager, name)


def test_cli_keeps_its_commands():
    parser = build_parser()
    subcommands = next(action for action in parser._actions if action.dest == "command")
    assert CLI_COMMANDS <= set(subcommands.choices)


def test_cli_entry_point_still_runs():
    result = subprocess.run([sys.executable, "-m", "celery_manager.cli", "--help"], capture_output=True, text=True, check=False)
    assert result.returncode == 0 and "doctor" in result.stdout
