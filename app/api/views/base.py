"""
app/api/views/base.py

Class-based views for FastAPI (which has no built-in equivalent).

    class ThingView(APIView):
        prefix = "/things"
        tags = ["things"]

        @route("GET", "", response_model=list[ThingOut])
        def list_things(self, session: Session = Depends(get_session)):
            ...

        @route("GET", "/{thing_id}", response_model=ThingOut)
        def get_thing(self, thing_id: int, ...):
            ...

    router = ThingView.as_router()      # then app.include_router(router)

Each handler is a normal method whose parameters (path/query/body/Depends)
are read by FastAPI exactly as for a plain function -- `self` is not part of
the request signature. Views hold no per-request state (the DB session etc.
arrive as dependencies), so a single instance safely serves all requests.

Routes are registered in the order the methods are defined, so a specific
path like "/me" can be placed before "/{id}".
"""

from fastapi import APIRouter

_ROUTE_SPEC = "_route_spec"


def route(method: str, path: str = "", **kwargs):
    """Mark a view method as an endpoint. `kwargs` go to add_api_route
    (response_model, status_code, summary, ...)."""

    def decorator(func):
        setattr(func, _ROUTE_SPEC, (method.upper(), path, kwargs))
        return func

    return decorator


class APIView:
    prefix: str = ""
    tags: list[str] | None = None

    @classmethod
    def as_router(cls) -> APIRouter:
        router = APIRouter(prefix=cls.prefix, tags=cls.tags)
        view = cls()

        # Walk base classes first so subclasses can override an endpoint by
        # redefining the method (the override keeps the original position).
        specs: dict[str, tuple] = {}
        for klass in reversed(cls.__mro__):
            for name, attr in vars(klass).items():
                spec = getattr(attr, _ROUTE_SPEC, None)
                if spec is not None:
                    specs[name] = spec

        for name, (method, path, kwargs) in specs.items():
            router.add_api_route(path, getattr(view, name), methods=[method], **kwargs)
        return router
