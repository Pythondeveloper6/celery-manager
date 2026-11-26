"""Redaction of task arguments, results, exceptions and tracebacks.

Every writer that stores a call or its outcome (decorator, events listener,
publish hook, `start_task`, reconciler, audit details, queue browser) goes
through the functions here, so one policy applies everywhere:

1. Values under a sensitive key (`CeleryManagerConfig.sensitive_keys`, regular
   expressions matched anywhere in the key, case-insensitively), or under a
   per-task path from `@track(redact_kwargs=...)`, become `[redacted]`.
2. Text (strings inside arguments, results, exceptions, tracebacks) is scrubbed
   of `key=value` / `key: value` pairs for those keys, bearer/basic
   credentials and signed-URL query parameters.
3. `payload_redactor(kind, task_name, value)` has the last word. If it raises,
   the value is withheld rather than stored unredacted.

All of it is idempotent, so a value redacted by one writer is left alone by
the next.
"""
from __future__ import annotations

import functools
import json
import logging
import re

logger = logging.getLogger(__name__)

PLACEHOLDER = "[redacted]"
WITHHELD = "[redaction failed; value withheld]"
DEFAULT_SENSITIVE_KEYS = (
    "password", "passwd", "secret", "token", "authorization", "cookie", "api.?key", "credential",
    "signature",
)
KINDS = ("args", "kwargs", "result", "exception", "traceback")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}")


@functools.lru_cache(maxsize=32)
def _patterns(keys: tuple[str, ...]):
    if not keys:
        return None, None
    union = "|".join(f"(?:{key})" for key in keys)
    key_re = re.compile(union, re.IGNORECASE)
    # `token=abc`, `"token": "abc"`, `Authorization: Bearer abc`, `?sig=abc`
    text_re = re.compile(
        r"(?i)((?:" + union + r")[\w-]*[\"']?\s*[:=]\s*[\"']?)(?!(?:(?:bearer|basic)\s+)?\[redacted\])(?:(?:bearer|basic)\s+)?[^\s,\"'&}\]]+"
    )
    return key_re, text_re


def _keys(config) -> tuple[str, ...]:
    return tuple(getattr(config, "sensitive_keys", DEFAULT_SENSITIVE_KEYS) or ()) if config is not None else DEFAULT_SENSITIVE_KEYS


def scrub_text(text: str, config=None, secrets: tuple[str, ...] = ()) -> str:
    """Scrub credentials out of free text; `secrets` are literal values to drop."""
    _, text_re = _patterns(_keys(config))
    text = _BEARER.sub(lambda m: m.group(1) + " " + PLACEHOLDER, text)
    if text_re is not None:
        text = text_re.sub(lambda m: m.group(1) + PLACEHOLDER, text)
    for secret in secrets:
        text = text.replace(secret, PLACEHOLDER)
    return text


def _walk(value, config, key_re, paths, path, secrets):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            here = path + (str(key),)
            if ".".join(here) in paths or (key_re is not None and key_re.search(str(key))):
                _collect(item, secrets)
                out[key] = PLACEHOLDER
            else:
                out[key] = _walk(item, config, key_re, paths, here, secrets)
        return out
    if isinstance(value, (list, tuple)):
        return [_walk(item, config, key_re, paths, path, secrets) for item in value]
    if isinstance(value, str):
        return scrub_text(value, config)
    return value


def _collect(value, secrets: list) -> None:
    """Remember redacted strings so they can be dropped from exception text too."""
    if isinstance(value, str) and len(value) >= 4 and value != PLACEHOLDER:
        secrets.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _collect(item, secrets)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect(item, secrets)


def _apply_hook(config, kind, task_name, value):
    hook = getattr(config, "payload_redactor", None)
    if hook is None:
        return value, False
    try:
        result = hook(kind, task_name, value)
        if kind in ("args", "kwargs"):
            expected = list if kind == "args" else dict
            if not isinstance(result, expected):
                raise TypeError(f"payload_redactor must return a {expected.__name__} for {kind}")
        elif not isinstance(result, str):
            raise TypeError("payload_redactor must return a string")
        return result, result != value
    except Exception:  # noqa: BLE001 - a failing redactor withholds, never stores raw
        logger.warning("celery-manager: payload_redactor failed; value withheld")
        return ([WITHHELD] if kind == "args" else {"withheld": WITHHELD} if kind == "kwargs" else WITHHELD), True


def _redact_json(text, kind, config, task_name, rule, secrets):
    """`(new_text, changed)` for a stored args/kwargs JSON document."""
    if not isinstance(text, str):
        return text, False
    key_re, _ = _patterns(_keys(config))
    all_args, paths = rule
    try:
        value = json.loads(text)
    except ValueError:
        # A truncated repr from an event: scrub as text, cannot be walked.
        new = scrub_text(text, config)
        new, hooked = _apply_text_hook(config, kind, task_name, new)
        return new, new != text or hooked
    if kind == "args" and all_args and isinstance(value, list):
        _collect(value, secrets)
        new_value = [PLACEHOLDER] * len(value)
    else:
        new_value = _walk(value, config, key_re, paths if kind == "kwargs" else (), (), secrets)
    new_value, hooked = _apply_hook(config, kind, task_name, new_value)
    if new_value == value and not hooked:
        return text, False
    return json.dumps(new_value, default=repr), True


def _apply_text_hook(config, kind, task_name, text):
    hook = getattr(config, "payload_redactor", None)
    if hook is None:
        return text, False
    try:
        result = hook(kind, task_name, text)
        if not isinstance(result, str):
            raise TypeError
        return result, result != text
    except Exception:  # noqa: BLE001
        logger.warning("celery-manager: payload_redactor failed; value withheld")
        return WITHHELD, True


def task_rule(task_name, app=None) -> tuple[bool, frozenset]:
    """Per-task `(redact_args, redact_kwargs paths)` set by `@track`."""
    try:
        from celery import current_app
        # `_tasks`, not `.tasks`: reading the public property finalizes the app
        task = (app or current_app)._tasks.get(task_name)
        options = getattr(getattr(task, "run", None), "_cm_redaction", None)
    except Exception:  # noqa: BLE001 - an unknown task just gets the global rules
        options = None
    return options if isinstance(options, tuple) and len(options) == 2 else (False, frozenset())


def redact_fields(config, task_name, fields: dict, app=None) -> dict:
    """Return `fields` (TaskRun column values) with every payload column redacted.

    Sets `args_redacted=True` when the stored arguments differ from the call.
    """
    if not any(k in fields for k in ("args", "kwargs", "exception", "traceback", "result_repr")):
        return fields
    out = dict(fields)
    rule = task_rule(task_name, app)
    secrets: list[str] = []
    changed = False
    for kind in ("args", "kwargs"):
        if out.get(kind) is not None:
            out[kind], did = _redact_json(out[kind], kind, config, task_name, rule, secrets)
            changed = changed or did
    for kind, column in (("result", "result_repr"), ("exception", "exception"), ("traceback", "traceback")):
        if isinstance(out.get(column), str):
            text = scrub_text(out[column], config, tuple(secrets))
            out[column], _ = _apply_text_hook(config, kind, task_name, text)
    if changed:
        out["args_redacted"] = True
    return out


_COLUMNS = ("args", "kwargs", "exception", "traceback", "result_repr")


def redact_run(config, run, app=None) -> None:
    """Redact a `TaskRun` in place (listener, reconciler, publish hook, seeds)."""
    current = {c: getattr(run, c) for c in _COLUMNS}
    for column, value in redact_fields(config, run.task_name, current, app).items():
        if column == "args_redacted":
            run.args_redacted = True
        elif value != current[column]:
            setattr(run, column, value)


def redact_call(config, task_name, args, kwargs, app=None):
    """Redact an in-memory call (audit details): `(args, kwargs)`."""
    out = redact_fields(config, task_name, {"args": json.dumps(args, default=repr), "kwargs": json.dumps(kwargs, default=repr)}, app)
    return json.loads(out["args"]), json.loads(out["kwargs"])


def redact_text(config, kind, task_name, text):
    return _apply_text_hook(config, kind, task_name, scrub_text(text, config))[0] if isinstance(text, str) else text


def view_payload(config, role, args, kwargs, traceback):
    """`(args, kwargs, traceback, args_restricted, traceback_restricted)` as `role` may see them.

    Roles below `args_view_role` get the masked shape (counts and types, never
    values); roles below `traceback_view_role` get no traceback.
    """
    from celery_manager.auth.roles import role_allows
    from celery_manager.control.masking import mask_call

    see_args = role_allows(role, config.args_view_role)
    see_trace = role_allows(role, config.traceback_view_role)
    if not see_args:
        def decode(text, empty):
            try:
                return json.loads(text)
            except (TypeError, ValueError):
                return text if text else empty
        masked = mask_call(decode(args, []), decode(kwargs, {}))
        args, kwargs = json.dumps(masked["args"]), json.dumps(masked["kwargs"])
    return args, kwargs, traceback if see_trace else None, not see_args, not see_trace
