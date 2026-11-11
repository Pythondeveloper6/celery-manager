"""Record what LLM calls made from a task cost: model, tokens, cost, latency.

    from celery_manager import record_llm_usage

    @app.task(bind=True)
    @track()
    def summarize(self, text):
        response = client.messages.create(model="...", max_tokens=512, messages=[...])
        record_llm_usage(response=response)        # model and tokens are read from it

or, to time the call and record failures too:

    from celery_manager.llm import llm_call
    with llm_call("my-model") as call:
        response = client.chat.completions.create(...)
        call.response = response

Each call is one row in ``llm_calls``, attached to the running task. Recording
never raises into the task: a failed write is logged and the task goes on.
Prompts and completions are never stored, only counts.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_warned = False
_MAX_MODEL = 128
_MAX_PROVIDER = 32
_PRICE_KEYS = {"input", "output", "cache_read", "cache_write"}


def validate_prices(prices) -> None:
    if not isinstance(prices, dict):
        raise ValueError("llm_prices must be a mapping of model id to prices")  # noqa: TRY004 - config validation contract
    for model, entry in prices.items():
        if not isinstance(model, str) or not model:
            raise ValueError("llm_prices keys must be non-empty model ids")
        if not isinstance(entry, dict) or not {"input", "output"} <= set(entry) or set(entry) - _PRICE_KEYS:
            raise ValueError(f"llm_prices[{model!r}] needs 'input' and 'output' (and optionally 'cache_read', 'cache_write')")
        for key, value in entry.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"llm_prices[{model!r}][{key!r}] must be a non-negative number (USD per million tokens)")


def compute_cost(prices: dict | None, model: str, input_tokens: int, output_tokens: int,
                 cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> float | None:
    """USD cost from per-million-token prices; None when the model has no price.
    The longest configured model-id prefix wins ("gpt-x" prices "gpt-x-2025-01")."""
    if not prices:
        return None
    matches = [name for name in prices if model.startswith(name)]
    if not matches:
        return None
    entry = prices[max(matches, key=len)]
    total = (input_tokens * entry["input"] + output_tokens * entry["output"]
             + cache_read_tokens * entry.get("cache_read", entry["input"])
             + cache_write_tokens * entry.get("cache_write", entry["input"]))
    return round(total / 1_000_000, 8)


def _get(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _int(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(number, 0)


def usage_from_response(response) -> dict:
    """Pull model, token counts and tool-call count out of an Anthropic- or
    OpenAI-style response object (or its dict form). Unknown shapes give {}."""
    usage = _get(response, "usage")
    if usage is None:
        return {}
    out: dict = {}
    model = _get(response, "model")
    if isinstance(model, str):
        out["model"] = model
    anthropic_in = _get(usage, "input_tokens")
    if anthropic_in is not None or _get(usage, "output_tokens") is not None:
        out["provider"] = "anthropic"
        out["input_tokens"] = _int(anthropic_in)
        out["output_tokens"] = _int(_get(usage, "output_tokens"))
        out["cache_read_tokens"] = _int(_get(usage, "cache_read_input_tokens"))
        out["cache_write_tokens"] = _int(_get(usage, "cache_creation_input_tokens"))
        content = _get(response, "content") or []
        out["tool_calls"] = sum(1 for block in content if _get(block, "type") == "tool_use") if isinstance(content, (list, tuple)) else 0
    else:
        out["provider"] = "openai"
        details = _get(usage, "prompt_tokens_details")
        cached = _int(_get(details, "cached_tokens"))
        # OpenAI counts cached tokens inside prompt_tokens; keep input = uncached.
        out["input_tokens"] = max(_int(_get(usage, "prompt_tokens")) - cached, 0)
        out["output_tokens"] = _int(_get(usage, "completion_tokens"))
        out["cache_read_tokens"] = cached
        choices = _get(response, "choices") or []
        first = choices[0] if isinstance(choices, (list, tuple)) and choices else None
        calls = _get(_get(first, "message"), "tool_calls") or []
        out["tool_calls"] = len(calls) if isinstance(calls, (list, tuple)) else 0
    return out


def _warn_once(message: str) -> None:
    global _warned
    if not _warned:
        _warned = True
        logger.warning("celery-manager: %s (logged once per process)", message)


def record_llm_usage(model: str | None = None, input_tokens: int | None = None, output_tokens: int | None = None, *,
                     provider: str | None = None, cache_read_tokens: int | None = None,
                     cache_write_tokens: int | None = None, tool_calls: int | None = None,
                     cost_usd: float | None = None, duration_ms: float | None = None,
                     status: str = "ok", response=None) -> None:
    """Record one LLM call against the running task. Explicit arguments win over
    values read from ``response``; ``cost_usd`` wins over ``config.llm_prices``."""
    try:
        _record(model, input_tokens, output_tokens, provider, cache_read_tokens, cache_write_tokens,
                tool_calls, cost_usd, duration_ms, status, response)
    except Exception:
        logger.warning("celery-manager: could not record LLM usage", exc_info=True)


def _record(model, input_tokens, output_tokens, provider, cache_read, cache_write, tool_calls,
            cost_usd, duration_ms, status, response) -> None:
    from celery_manager import decorator
    from celery_manager.db.llm import LlmCall
    from celery_manager.progress import _resolve_run

    found = usage_from_response(response) if response is not None else {}
    model = model or found.get("model")
    if not model:
        _warn_once("record_llm_usage() needs a model (pass model=, or a response that carries one)")
        return
    sessions = decorator._sessions()
    if sessions is None:
        _warn_once("record_llm_usage() ran in a process that never called celery_manager.setup(config)")
        return

    def pick(explicit, key):
        return _int(explicit if explicit is not None else found.get(key))

    counts = {
        "input_tokens": pick(input_tokens, "input_tokens"), "output_tokens": pick(output_tokens, "output_tokens"),
        "cache_read_tokens": pick(cache_read, "cache_read_tokens"),
        "cache_write_tokens": pick(cache_write, "cache_write_tokens"), "tool_calls": pick(tool_calls, "tool_calls"),
    }
    config = decorator._settings()
    if cost_usd is not None and (isinstance(cost_usd, bool) or not isinstance(cost_usd, (int, float))
                                 or not math.isfinite(cost_usd) or cost_usd < 0):
        cost_usd = None
    if cost_usd is None:
        cost_usd = compute_cost(getattr(config, "llm_prices", None), model, counts["input_tokens"],
                                counts["output_tokens"], counts["cache_read_tokens"], counts["cache_write_tokens"])
    task_id, task_name = _resolve_run()
    if task_id and not task_name:
        task_name = _name_of_run(sessions, task_id)
    provider = provider or found.get("provider")
    row = LlmCall(
        task_id=task_id, task_name=task_name, provider=str(provider)[:_MAX_PROVIDER] if provider else None,
        model=str(model)[:_MAX_MODEL], cost_usd=cost_usd,
        duration_ms=float(duration_ms) if isinstance(duration_ms, (int, float)) and math.isfinite(duration_ms) and duration_ms >= 0 else None,
        status="error" if status == "error" else "ok", created_at=datetime.now(timezone.utc), **counts,
    )
    with sessions() as session:
        session.add(row)
        session.commit()


def _name_of_run(sessions, task_id: str) -> str | None:
    from celery_manager.db.models import TaskRun
    try:
        with sessions() as session:
            return session.query(TaskRun.task_name).filter(TaskRun.task_id == task_id).scalar()
    except Exception:  # noqa: BLE001
        return None


class _Call:
    def __init__(self, model, provider):
        self.model = model
        self.provider = provider
        self.response = None
        self.fields: dict = {}

    def set(self, **fields) -> None:
        """Extra values to record (input_tokens=..., cost_usd=..., and so on)."""
        self.fields.update(fields)


class llm_call:
    """Time an LLM call and record it on exit, as an error if the block raised.

    Assign ``call.response`` (or use ``call.set(...)``) inside the block."""

    def __init__(self, model: str | None = None, *, provider: str | None = None):
        self._call = _Call(model, provider)

    def __enter__(self) -> _Call:
        self._start = time.perf_counter()
        return self._call

    def __exit__(self, exc_type, exc, tb) -> bool:
        call = self._call
        record_llm_usage(call.model, provider=call.provider, response=call.response,
                         duration_ms=(time.perf_counter() - self._start) * 1000,
                         status="error" if exc_type else "ok", **call.fields)
        return False
