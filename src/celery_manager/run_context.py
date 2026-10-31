"""Bounded host-supplied context shared by producer hooks and native observers."""
import json

from celery_manager.observation import _clean, _identifier

DEFAULT_KEYS = ("integration", "request_id", "trace_id", "environment", "deployment_id", "resource_id")


def normalize_context(value, config=None):
    if not isinstance(value, dict) or set(value) - {"tenant_id", "correlation_id", "operation_key", "metadata"}:
        raise ValueError("Run context accepts tenant_id, correlation_id, operation_key and metadata")
    result = {key: _identifier(value.get(key), key) for key in ("tenant_id", "correlation_id", "operation_key")}
    metadata = value.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("Run metadata must be a mapping")  # noqa: TRY004 - context validation contract
    keys = getattr(config, "observer_metadata_keys", DEFAULT_KEYS)
    if keys is not None:
        metadata = {key: item for key, item in metadata.items() if key in keys}
    metadata = _clean(metadata)
    limit = getattr(config, "observer_context_max_bytes", 16384)
    if len(json.dumps(metadata, ensure_ascii=True).encode()) > limit:
        metadata = {"_truncated": True}
    result["metadata"] = metadata
    return result


def storage_fields(value):
    return {"tenant_id": value.get("tenant_id"), "correlation_id": value.get("correlation_id"),
            "operation_key": value.get("operation_key"), "context_json": json.dumps(value["metadata"], ensure_ascii=True)}


def context_for_run(run, config=None):
    """Recover trusted producer context for an operator retry.

    A legacy row with no context or identity has no context to propagate.
    Malformed persisted JSON is refused rather than silently losing tenant
    assignment. Pass config to apply its current metadata allowlist/limits.
    """
    identity = {key: _identifier(getattr(run, key, None), key)
                for key in ("tenant_id", "correlation_id", "operation_key")}
    raw = getattr(run, "context_json", None)
    if raw is None and all(value is None for value in identity.values()):
        return None
    try:
        metadata = json.loads(raw) if raw is not None else {}
    except (ValueError, TypeError) as exc:
        raise ValueError("Stored run context is not valid JSON") from exc
    if not isinstance(metadata, dict):
        raise ValueError("Stored run context metadata must be a mapping")  # noqa: TRY004 - invalid stored JSON contract
    value = {**identity, "metadata": metadata}
    if config is not None:
        return normalize_context(value, config)
    # Already-persisted custom approved keys remain usable by direct control
    # callers without config; downstream publish normalization still applies.
    value["metadata"] = _clean(metadata)
    if len(json.dumps(value["metadata"], ensure_ascii=True).encode()) > 16384:
        value["metadata"] = {"_truncated": True}
    return value


def merge_storage_context(row, normalized):
    """Bind producer identity once, under the caller's durable row lock.

    context_json is the initialization marker, so an explicitly bound NULL
    tenant/correlation/key cannot be reassigned on a later publish callback.
    """
    if row.source != "celery":
        raise ValueError("Run source collision")
    values = storage_fields(normalized)
    initialized = row.context_json is not None
    for key in ("tenant_id", "correlation_id", "operation_key"):
        existing = getattr(row, key)
        if (initialized and existing != values[key]) or (not initialized and existing not in (None, values[key])):
            raise ValueError("Run context identity collision")
    for key, value in values.items():
        if getattr(row, key) is None:
            setattr(row, key, value)
