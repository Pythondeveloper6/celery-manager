"""Record Anthropic and OpenAI SDK calls without code in the task body.

    from celery_manager.contrib import llm
    llm.instrument()             # in the worker, after celery_manager.setup()

Wraps the SDKs' message / chat-completion `create` calls (sync and async) and
feeds `record_llm_usage`: model, token counts, duration and errors. Prompts
and completions are never stored.
"""
from __future__ import annotations

import importlib
import time

_patched: list[tuple[type, str, object]] = []

_TARGETS = {
    "anthropic": (("anthropic.resources.messages", "Messages", False),
                  ("anthropic.resources.messages", "AsyncMessages", True)),
    "openai": (("openai.resources.chat.completions", "Completions", False),
               ("openai.resources.chat.completions", "AsyncCompletions", True)),
}


def _record(provider, kwargs, started, response=None, error=None):
    from celery_manager.llm import record_llm_usage
    # shortcut: a streamed response (stream=True) records model and time to
    # the first byte, not tokens; wrap the stream if token counts are needed.
    streamed = bool(kwargs.get("stream"))
    record_llm_usage(kwargs.get("model"), provider=provider,
                     response=None if streamed or error is not None else response,
                     duration_ms=(time.perf_counter() - started) * 1000,
                     status="error" if error is not None else "ok")


def _wrap(cls, provider, is_async):
    original = cls.create
    if is_async:
        async def create(self, *args, **kwargs):
            started = time.perf_counter()
            try:
                response = await original(self, *args, **kwargs)
            except Exception as exc:
                _record(provider, kwargs, started, error=exc)
                raise
            _record(provider, kwargs, started, response=response)
            return response
    else:
        def create(self, *args, **kwargs):
            started = time.perf_counter()
            try:
                response = original(self, *args, **kwargs)
            except Exception as exc:
                _record(provider, kwargs, started, error=exc)
                raise
            _record(provider, kwargs, started, response=response)
            return response
    cls.create = create
    _patched.append((cls, "create", original))


def instrument(anthropic: bool = True, openai: bool = True) -> list[str]:
    """Patch the installed SDKs once per process. Returns what was patched."""
    done = []
    for provider, wanted in (("anthropic", anthropic), ("openai", openai)):
        if not wanted or any(p[0].__module__.startswith(provider) for p in _patched):
            continue
        try:
            for module_name, class_name, is_async in _TARGETS[provider]:
                _wrap(getattr(importlib.import_module(module_name), class_name), provider, is_async)
        except (ImportError, AttributeError):
            continue
        done.append(provider)
    return done


def uninstrument() -> None:
    while _patched:
        cls, name, original = _patched.pop()
        setattr(cls, name, original)
