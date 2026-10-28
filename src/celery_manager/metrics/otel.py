"""OpenTelemetry export of the same snapshot `/metrics` serves.

Needs the optional extra: `pip install "celery-manager[otel]"`. The
OpenTelemetry API is imported only when `register` is called, so the rest of
celery-manager never depends on it.

Every metric becomes an observable gauge. Observation callbacks read the
collector's cached snapshot (`metrics_cache_seconds`), so an exporter that
collects every few seconds shares one database pass with Prometheus scrapes.
"""

from __future__ import annotations

from celery_manager.metrics import METRIC_NAMES, MetricsCollector


def register(meter_provider=None, *, app=None, collector: MetricsCollector | None = None):
    """Register the gauges on `meter_provider` (the global one when None).

    Pass the app `create_app()` returned (its collector is reused) or a
    `MetricsCollector` of your own. Returns the meter. Call it once.
    """
    try:
        from opentelemetry import metrics as otel_metrics
        from opentelemetry.metrics import Observation
    except ImportError as exc:
        raise ImportError(
            "OpenTelemetry export needs opentelemetry-api. "
            'Install it with: pip install "celery-manager[otel]"'
        ) from exc

    if collector is None:
        if app is None:
            raise ValueError("pass app=<the app from create_app()> or collector=<a MetricsCollector>")
        collector = app.state.cm_metrics
    meter = (
        meter_provider.get_meter("celery_manager")
        if meter_provider is not None
        else otel_metrics.get_meter("celery_manager")
    )

    for name in METRIC_NAMES:
        meter.create_observable_gauge(name, callbacks=[_callback(collector, name, Observation)])
    return meter


def _callback(collector: MetricsCollector, name: str, observation_cls):
    def observe(options):
        for family in collector.snapshot():
            if family.name == name:
                return [observation_cls(value, attributes=dict(labels)) for labels, value in family.samples]
        return []

    return observe
