"""Masked previews of task arguments.

Views that read arguments straight off the broker or out of a worker's
memory (delayed tasks today) show their *shape* by default — how many
positional arguments, which keyword names, and the type of each value — and
never the values themselves. The values are what may hold an email address,
a token or a customer id; the shape is what an operator needs to tell two
calls of the same task apart at a glance. Seeing the values is an explicit,
role-checked request at the API layer, not something a listing hands out.
"""

from __future__ import annotations


def _label(value) -> str:
    """`"str"`, `"int"`, `"list[3]"`, `"dict[2]"`, `"None"`, ... — enough to
    recognise a call, nothing of its content."""
    if value is None:
        return "None"
    if isinstance(value, (list, tuple, dict, set)):
        return f"{type(value).__name__}[{len(value)}]"
    return type(value).__name__


def mask_call(args, kwargs) -> dict:
    """`{"args": ["str", "int"], "kwargs": {"user_id": "int"}}` for a call's
    arguments. Anything that isn't a list/tuple (args) or dict (kwargs) —
    a truncated repr string from an old worker, say — becomes a single
    label rather than being guessed at."""
    if isinstance(args, (list, tuple)):
        masked_args = [_label(a) for a in args]
    elif args in (None, ""):
        masked_args = []
    else:
        masked_args = [_label(args)]
    if isinstance(kwargs, dict):
        masked_kwargs = {str(k): _label(v) for k, v in kwargs.items()}
    else:
        masked_kwargs = {} if kwargs in (None, "") else {"?": _label(kwargs)}
    return {"args": masked_args, "kwargs": masked_kwargs}
