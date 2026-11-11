# LLM usage

Tasks that call a language model can record each call: model, token counts,
cost and latency. celery-manager attaches the calls to the run, shows totals on
the **Insights** page and the run's detail page, exposes them through the REST
API and MCP, and can alert on spend. **Prompts and completions are never
stored**, only counts.

## Recording calls

```python
from celery_manager import record_llm_usage, track

@app.task(bind=True)
@track()
def summarize(self, text):
    response = client.messages.create(model="...", max_tokens=512, messages=[...])
    record_llm_usage(response=response)      # model and tokens come from the response
    return response.content[0].text
```

`response` may be an Anthropic- or OpenAI-style object or its dict form. Model,
token counts (including cached input) and the number of tool calls are read from
it. Pass values yourself to override, or when you have no response object:

```python
record_llm_usage("my-model", input_tokens=812, output_tokens=140, cost_usd=0.0021)
```

To time a call and record failures too:

```python
from celery_manager.llm import llm_call

with llm_call("my-model") as call:
    call.response = client.chat.completions.create(...)
```

If the block raises, the call is recorded with status `error` and the exception
propagates. Recording never raises into your task: a failed write is logged and
the task carries on. The process needs `celery_manager.setup(config)`, like
`@track`.

## Cost

Cost is the `cost_usd` you pass, otherwise it is computed from `llm_prices` in
your config (USD per million tokens). Nothing is built in, because prices
change:

```python
CeleryManagerConfig(
    ...,
    llm_prices={
        "claude-sonnet-4": {"input": 3.0, "output": 15.0, "cache_read": 0.3},
    },
)
```

The longest matching model-id prefix wins. A model with no price has no cost;
the dashboard counts those calls as unpriced rather than showing $0.

## Where it shows up

| Where | What |
|---|---|
| Insights, Trends tab | Calls, tokens, cost, errors, cost over time, by model, by task |
| Run detail | The run's calls and totals |
| `GET /api/llm/usage?range=24h&task_name=...` | The same summary as JSON |
| `GET /api/llm/runs/{task_id}` | One run's calls |
| MCP | `llm_usage`, `run_llm_usage` |
| Alerts | The `llm_cost` rule |

### Alert on spend

The `llm_cost` rule fires per task name when its recorded cost in the last
`window_minutes` reaches `usd` (defaults 10 USD, 60 minutes). The rule's target
is a task name or glob, `*` for all. Calls without a cost are ignored.

## Retention

`llm_call_retention_days` (default 90, `0` keeps everything) controls how long
rows are kept; `celery-manager prune` and the dashboard's retention prune apply
it.
