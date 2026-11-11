"""Alert on LLM spend: a task's recorded cost over a window crossing a budget."""
from datetime import timedelta

from sqlalchemy import func

from celery_manager.db.llm import LlmCall

PARAMS = {
    "llm_cost": {"usd": (float, 10.0, 0.0001, 1_000_000_000.0), "window_minutes": (int, 60, 1, 10080)},
}


def check(rule, params, probe):
    from celery_manager.alerts.evaluator import Observation, _matches
    since = probe.now - timedelta(minutes=params["window_minutes"])
    rows = (probe.session.query(LlmCall.task_name, func.sum(LlmCall.cost_usd), func.count())
            .filter(LlmCall.created_at >= since, LlmCall.created_at <= probe.now, LlmCall.cost_usd.isnot(None),
                    LlmCall.task_name.isnot(None))
            .group_by(LlmCall.task_name).all())
    observation = Observation()
    for task_name, cost, calls in rows:
        if _matches(rule.target, task_name) and cost >= params["usd"]:
            observation.firing[task_name] = {
                "cost_usd": round(cost, 4), "calls": calls, "threshold": params["usd"],
                "window_minutes": params["window_minutes"],
            }
    return observation
