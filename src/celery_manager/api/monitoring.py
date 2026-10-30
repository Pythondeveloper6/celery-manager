"""Restrict observation-only deployments to capabilities they actually own."""
from starlette.responses import JSONResponse


class MonitoringOnlyMiddleware:
    def __init__(self, app, config):
        self.app, self.config = app, config

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.config.celery_app is None:
            path = scope["path"]
            root = scope.get("root_path", "")
            if root and path.startswith(root):
                path = path[len(root):]
            path = path.replace("/api/v1/", "/api/", 1)
            blocked = any(path == prefix or path.startswith(prefix + "/") for prefix in (
                "/api/schedules", "/api/schedule-reconciliation", "/api/queues", "/api/workers",
                "/api/rabbitmq", "/api/delayed", "/api/tasks/rate-limit", "/api/locks"))
            blocked = blocked or (path.startswith("/api/tasks") and scope["method"] not in ("GET", "HEAD", "OPTIONS"))
            blocked = blocked or path == "/api/dead-letters/rerun"
            if blocked:
                await JSONResponse({"detail": "This deployment observes host jobs; Celery execution controls are unavailable."}, status_code=409)(scope, receive, send)
                return
        await self.app(scope, receive, send)
