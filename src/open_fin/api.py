"""Authenticated single-owner HTTP API. Browser automation belongs to the worker."""

import secrets
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from open_fin import __version__
from open_fin.config import Settings
from open_fin.models import (
    ConnectionList,
    ConnectionView,
    CreateConnection,
    Currency,
    MovementPage,
    ProductPage,
    Source,
    SyncView,
)
from open_fin.storage import Store, StoreError

bearer = HTTPBearer(auto_error=False)


def authorize(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not secrets.compare_digest(
            credentials.credentials.encode("utf-8"),
            request.app.state.settings.api_token.encode("utf-8"),
        )
    ):
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})


def store(request: Request) -> Store:
    return request.app.state.store


Database = Annotated[Store, Depends(store)]


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings or Settings.from_env()
        configured = app.state.settings
        app.state.store = Store(configured.database_path, configured.encryption_key)
        yield

    app = FastAPI(
        title="Open Fin",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        description="Single-owner banking API. Santander Chile live support is experimental.",
        license_info={"name": "AGPL-3.0-only", "url": "https://www.gnu.org/licenses/agpl-3.0.html"},
    )

    @app.middleware("http")
    async def private_cache(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, error: RequestValidationError):
        # FastAPI's default includes input values, potentially including an entire credential body.
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]}
                    for item in error.errors()
                ]
            },
        )

    @app.exception_handler(StoreError)
    async def storage_error(_request: Request, error: StoreError):
        status = {"not_found": 404, "sync_in_progress": 409, "cooldown": 429, "mode_mismatch": 409}[
            error.code
        ]
        headers = {"Retry-After": str(error.retry_after)} if error.retry_after else None
        return JSONResponse(status_code=status, content={"error_code": error.code}, headers=headers)

    @app.get("/health")
    def health(request: Request):
        return {
            "status": "ok",
            "mode": request.app.state.settings.connector_mode,
            "version": __version__,
        }

    @app.get("/")
    def source(request: Request):
        return {
            "name": "Open Fin",
            "version": __version__,
            "license": "AGPL-3.0-only",
            "source": request.app.state.settings.source_url,
        }

    auth = [Depends(authorize)]

    @app.get("/docs", include_in_schema=False, dependencies=auth)
    def documentation():
        return get_swagger_ui_html(openapi_url="/openapi.json", title="Open Fin API")

    @app.get("/openapi.json", include_in_schema=False, dependencies=auth)
    def openapi():
        return app.openapi()

    @app.post("/v1/connections", status_code=201, dependencies=auth, response_model=ConnectionView)
    def create_connection(payload: CreateConnection, db: Database, request: Request):
        if request.app.state.settings.connector_mode == "demo" and (
            payload.credentials.rut.get_secret_value() != "11111111-1"
            or payload.credentials.password.get_secret_value() != "demo"
        ):
            raise HTTPException(422, "Demo mode only accepts the documented synthetic credentials")
        return db.create_connection(payload, mode=request.app.state.settings.connector_mode)

    @app.get("/v1/connections", dependencies=auth, response_model=ConnectionList)
    def list_connections(db: Database):
        return {"items": db.connections()}

    @app.get("/v1/connections/{connection_id}", dependencies=auth, response_model=ConnectionView)
    def get_connection(connection_id: str, db: Database):
        return db.connection(connection_id)

    @app.delete("/v1/connections/{connection_id}", status_code=204, dependencies=auth)
    def delete_connection(connection_id: str, db: Database):
        db.delete_connection(connection_id)
        return Response(status_code=204)

    @app.post(
        "/v1/connections/{connection_id}/sync",
        status_code=202,
        dependencies=auth,
        response_model=SyncView,
    )
    def synchronize(connection_id: str, db: Database, request: Request):
        if db.connection(connection_id)["mode"] != request.app.state.settings.connector_mode:
            raise StoreError("mode_mismatch")
        return db.enqueue(connection_id, request.app.state.settings.cooldown_seconds)

    @app.get("/v1/syncs/{sync_id}", dependencies=auth, response_model=SyncView)
    def synchronization(sync_id: str, db: Database):
        return db.sync(sync_id)

    @app.get("/v1/accounts", dependencies=auth, response_model=ProductPage)
    def accounts(
        db: Database,
        request: Request,
        connection_id: str | None = None,
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        return db.query(
            "accounts",
            connection_id=connection_id,
            limit=limit,
            offset=offset,
            mode=request.app.state.settings.connector_mode,
        )

    @app.get("/v1/cards", dependencies=auth, response_model=ProductPage)
    def cards(
        db: Database,
        request: Request,
        connection_id: str | None = None,
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        return db.query(
            "cards",
            connection_id=connection_id,
            limit=limit,
            offset=offset,
            mode=request.app.state.settings.connector_mode,
        )

    @app.get("/v1/movements", dependencies=auth, response_model=MovementPage)
    def movements(
        db: Database,
        request: Request,
        connection_id: str | None = None,
        product_id: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        currency: Currency | None = None,
        source: Source | None = None,
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        if from_date and to_date and from_date > to_date:
            raise HTTPException(422, "from_date must precede or equal to_date")
        return db.query(
            "movements",
            connection_id=connection_id,
            product_id=product_id,
            from_date=from_date.isoformat() if from_date else None,
            to_date=to_date.isoformat() if to_date else None,
            currency=currency,
            source=source,
            limit=limit,
            offset=offset,
            mode=request.app.state.settings.connector_mode,
        )

    return app


app = create_app()
