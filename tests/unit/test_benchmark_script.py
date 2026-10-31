"""scripts/benchmark.py keeps working as the code it measures changes."""
import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "benchmark.py"


def test_benchmark_runs_end_to_end(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("cm_benchmark", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--db-url", f"sqlite:///{tmp_path / 'b.db'}", "--events", "30", "--requests", "2"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["listener"]["events"] == 30 and report["listener"]["events_per_second"] > 0
    assert set(report["dashboard"]) >= {"/api/tasks?limit=50", "/api/analytics/overview"}
