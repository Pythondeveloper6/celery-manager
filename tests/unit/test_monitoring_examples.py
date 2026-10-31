"""The shipped Grafana dashboard and Prometheus rules only use real metrics."""
import json
import re
from pathlib import Path

import celery_manager.metrics as metrics_module

ROOT = Path(__file__).resolve().parents[2] / "examples" / "monitoring"
METRIC = re.compile(r"\bcelery_manager_[a-z0-9_]+")


def exported_names():
    source = Path(metrics_module.__file__).read_text()
    names = set(METRIC.findall(source))
    # Windowed histograms are exposed as _bucket/_sum/_count gauges.
    for base in [name for name in names if name.endswith("_seconds")]:
        names |= {base + suffix for suffix in ("_bucket", "_sum", "_count")}
    return names


def referenced(text):
    return {name for name in METRIC.findall(text)}


def test_dashboard_is_valid_json_and_queries_known_metrics():
    dashboard = json.loads((ROOT / "grafana-dashboard.json").read_text())
    exprs = [target["expr"] for panel in dashboard["panels"] for target in panel["targets"]]
    assert exprs
    unknown = set().union(*(referenced(expr) for expr in exprs)) - exported_names()
    assert not unknown, unknown
    ids = [panel["id"] for panel in dashboard["panels"]]
    assert len(ids) == len(set(ids))


def test_alert_rules_reference_known_metrics_and_are_well_formed():
    text = (ROOT / "prometheus-rules.yml").read_text()
    exprs = re.findall(r"^\s+expr: (.+)$", text, re.MULTILINE)
    alerts = re.findall(r"^\s+- alert: (\w+)$", text, re.MULTILINE)
    assert len(exprs) == len(alerts) >= 8
    assert len(set(alerts)) == len(alerts)
    unknown = set().union(*(referenced(expr) for expr in exprs)) - exported_names()
    assert not unknown, unknown
    assert "\t" not in text
