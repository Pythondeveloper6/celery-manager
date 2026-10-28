"""Public dashboard links, including mounts and arbitrary task identifiers."""
from urllib.parse import quote


def task_url(base_url: str, task_id: str) -> str:
    return base_url.split("#", 1)[0].rstrip("/") + "/#/tasks/" + quote(str(task_id), safe="")
