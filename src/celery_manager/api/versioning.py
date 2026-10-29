"""Stable v1 aliases of the existing API, sharing endpoints and role checks."""
from fastapi import APIRouter


def install_v1(app, routes):
    router = APIRouter()
    for path, route, inherited_dependencies in routes:
        if not path.startswith("/api/"):
            continue
        router.add_api_route(
            "/api/v1/" + path[len("/api/"):], route.endpoint,
            methods=route.methods, response_model=route.response_model,
            status_code=route.status_code, response_class=route.response_class,
            dependencies=[*inherited_dependencies, *route.dependencies],
            name="v1_" + route.name, operation_id="v1_" + route.unique_id,
            summary=route.summary, description=route.description, tags=route.tags,
            responses=route.responses,
            response_model_exclude_none=route.response_model_exclude_none,
            response_model_exclude_unset=route.response_model_exclude_unset,
            response_model_exclude_defaults=route.response_model_exclude_defaults,
            include_in_schema=route.include_in_schema,
        )
    app.include_router(router)
