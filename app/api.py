"""FastAPI application wiring: routing, stable error codes, repository."""

from __future__ import annotations

import contextlib
import json
import os
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .repository import InMemoryRepository
from .repository.base import Repository
from .service import PlanService, ServiceError
from .validation import ValidationError


class JsonParseError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _DuplicateKey(Exception):
    pass


def _reject_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


async def json_body(request: Request) -> Any:
    """Parse a JSON object body with stable error codes.

    An empty body is accepted and treated as ``{}`` (action endpoints
    such as compute/adopt take no parameters).  Duplicate object keys are
    rejected explicitly; the top-level payload must be a JSON object.
    """
    raw = await request.body()
    if not raw:
        return {}
    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_dupes)
    except UnicodeDecodeError as exc:
        raise JsonParseError("INVALID_JSON", "body is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise JsonParseError(
            "INVALID_JSON", f"body is not valid JSON: {exc.msg}"
        ) from exc
    except _DuplicateKey as exc:
        raise JsonParseError(
            "DUPLICATE_JSON_KEY", f"duplicate key in JSON object: {exc.args[0]}"
        ) from exc
    if not isinstance(doc, dict):
        raise JsonParseError("INVALID_JSON", "request body must be a JSON object")
    return doc


def require_empty_object(doc: Any = Depends(json_body)) -> dict:
    """Dependency for action endpoints that accept no parameters."""
    if doc:
        raise JsonParseError(
            "UNEXPECTED_FIELDS", "this endpoint takes no parameters; send {}"
        )
    return {}


def create_app(repository: Repository | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        if repository is not None:
            app.state.repo = repository
            app.state.repo_owned = False
        else:
            app.state.repo_owned = True
            dsn = os.environ.get("DATABASE_URL")
            if dsn:
                # Imported lazily so unit tests never need psycopg.
                from .repository.postgres import PostgresRepository

                app.state.repo = await PostgresRepository.connect(dsn)
            else:
                app.state.repo = InMemoryRepository()
        try:
            yield
        finally:
            if app.state.repo is not None and app.state.repo_owned:
                await app.state.repo.close()
            app.state.repo = None

    app = FastAPI(
        title="Cleanroom Ventilation Minimum-Cut Service",
        version="1.0.0",
        lifespan=lifespan,
    )

    def service(request: Request) -> PlanService:
        return PlanService(request.app.state.repo)

    def _error(status_code: int, code: str, message: str) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content={"error": {"code": code, "message": message}},
        )

    @app.exception_handler(JsonParseError)
    async def _on_json_error(_request: Request, exc: JsonParseError):
        status = 400 if exc.code in {"MISSING_BODY", "INVALID_JSON"} else 422
        return _error(status, exc.code, str(exc))

    @app.exception_handler(ValidationError)
    async def _on_validation_error(_request: Request, exc: ValidationError):
        return _error(422, exc.code, exc.message)

    @app.exception_handler(ServiceError)
    async def _on_service_error(_request: Request, exc: ServiceError):
        return _error(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _on_fastapi_validation(_request: Request, _exc: RequestValidationError):
        return _error(400, "INVALID_REQUEST", "malformed request")

    @app.exception_handler(Exception)
    async def _on_unexpected(_request: Request, exc: Exception):
        # Last-resort guard so clients always receive the documented JSON
        # error envelope instead of an HTML page.
        return _error(500, "INTERNAL_ERROR", "internal server error")

    @app.get("/healthz")
    async def healthz(request: Request) -> dict:
        return {"status": "ok", "storage": _storage_name(request.app.state.repo)}

    @app.put("/api/plans/{plan_id}")
    async def save_plan(
        plan_id: str,
        doc: Any = Depends(json_body),
        svc: PlanService = Depends(service),
    ) -> dict:
        record = await svc.save_plan(plan_id, doc)
        return {
            "plan_id": record.plan_id,
            "version": record.version,
            "content_hash": record.content_hash,
            "plan": record.plan,
        }

    @app.get("/api/plans/{plan_id}")
    async def get_plan(
        plan_id: str, svc: PlanService = Depends(service)
    ) -> dict:
        record = await svc.get_plan(plan_id)
        return {
            "plan_id": record.plan_id,
            "version": record.version,
            "content_hash": record.content_hash,
            "plan": record.plan,
        }

    @app.post("/api/plans/{plan_id}/compute")
    async def compute_plan(
        plan_id: str,
        _empty: dict = Depends(require_empty_object),
        svc: PlanService = Depends(service),
    ) -> dict:
        return await svc.compute(plan_id)

    @app.post("/api/results/{computation_id}/adopt")
    async def adopt_result(
        computation_id: str,
        _empty: dict = Depends(require_empty_object),
        svc: PlanService = Depends(service),
    ) -> dict:
        adopted = await svc.adopt(computation_id)
        return {
            "computation_id": adopted.computation_id,
            "adopted_at": adopted.adopted_at,
            "snapshot": adopted.snapshot,
        }

    @app.get("/api/adopted")
    async def get_adopted(svc: PlanService = Depends(service)) -> dict:
        adopted = await svc.get_adopted()
        return {
            "computation_id": adopted.computation_id,
            "adopted_at": adopted.adopted_at,
            "snapshot": adopted.snapshot,
        }

    return app


def _storage_name(repo: Repository | None) -> str:
    if repo is None:
        return "none"
    cls = type(repo).__name__
    return {
        "InMemoryRepository": "memory",
        "PostgresRepository": "postgresql",
    }.get(cls, cls)


app = create_app()
