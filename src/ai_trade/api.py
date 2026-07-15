from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import APIKeyCookie
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from .brokers.ibkr import IBKRBroker
from .config import AppSettings
from .db import Database, DatabaseRepository
from .engine import TradingEngine
from .strategies import MeanReversionStrategy, MomentumStrategy

COOKIE_NAME = "ai_trade_session"
cookie_scheme = APIKeyCookie(name=COOKIE_NAME, auto_error=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class OperatorRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


def create_app(
    engine: TradingEngine,
    operator_password: str,
    *,
    lifecycle: Any | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def default_lifespan(_: FastAPI) -> AsyncIterator[None]:
        await engine.start()
        try:
            yield
        finally:
            await engine.close()

    app = FastAPI(
        title="AI Trade Operator",
        version="0.1.0",
        lifespan=lifecycle or default_lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=secrets.token_hex(32),
        session_cookie=COOKIE_NAME,
        same_site="strict",
        https_only=False,
    )
    app.state.engine = engine
    app.state.operator_password = operator_password

    async def authenticated(request: Request, _: str | None = Depends(cookie_scheme)) -> str:
        if not request.session.get("authenticated"):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
        return "operator"

    async def authorized_command(
        request: Request,
        payload: OperatorRequest,
        actor: Annotated[str, Depends(authenticated)],
    ) -> tuple[str, OperatorRequest]:
        csrf = request.headers.get("X-CSRF-Token")
        if not csrf or not secrets.compare_digest(csrf, request.session.get("csrf", "")):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid CSRF token")
        return actor, payload

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "login.html", {})

    @app.post("/login")
    async def login(request: Request, password: Annotated[str, Form()]) -> RedirectResponse:
        if not secrets.compare_digest(password, app.state.operator_password):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid password")
        request.session["authenticated"] = True
        request.session["csrf"] = secrets.token_urlsafe(32)
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/logout")
    async def logout(request: Request) -> RedirectResponse:
        request.session.clear()
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, _: str = Depends(authenticated)) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "csrf": request.session["csrf"],
                "state": engine.state,
                "portfolio": engine.ledger.snapshot(),
            },
        )

    @app.get("/api/health")
    async def health(_: str = Depends(authenticated)) -> dict[str, Any]:
        return {
            "state": engine.state.model_dump(mode="json"),
            "broker_account": engine.broker.account_id,
            "universe_version": engine.universe_version,
            "latest_data_at": (
                engine.latest_data_at.isoformat() if engine.latest_data_at else None
            ),
        }

    @app.get("/api/portfolio")
    async def portfolio(_: str = Depends(authenticated)) -> dict[str, Any]:
        return engine.ledger.snapshot().model_dump(mode="json")

    @app.get("/api/orders")
    async def orders(_: str = Depends(authenticated)) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in engine.orders.all()]

    @app.get("/api/positions")
    async def positions(_: str = Depends(authenticated)) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in engine.ledger.snapshot().positions]

    @app.get("/api/strategies")
    async def strategies(_: str = Depends(authenticated)) -> list[dict[str, str]]:
        return [{"name": item.name, "allocation": "25% max gross"} for item in engine.strategies]

    @app.get("/api/decisions")
    async def decisions(_: str = Depends(authenticated)) -> list[dict[str, object]]:
        return [item for item in engine.audit_log() if item["event_type"] == "RISK_REJECTED"]

    @app.get("/api/audit")
    async def audit(_: str = Depends(authenticated)) -> tuple[dict[str, object], ...]:
        return engine.audit_log()

    @app.post("/api/operator/arm")
    async def arm(
        command: tuple[str, OperatorRequest] = Depends(authorized_command),
    ) -> dict[str, Any]:
        actor, _ = command
        return (await engine.arm(actor)).model_dump(mode="json")

    @app.post("/api/operator/disarm")
    async def disarm(
        command: tuple[str, OperatorRequest] = Depends(authorized_command),
    ) -> dict[str, Any]:
        actor, payload = command
        return (await engine.disarm(payload.reason, actor)).model_dump(mode="json")

    @app.post("/api/operator/cancel-all", status_code=status.HTTP_204_NO_CONTENT)
    async def cancel_all(
        command: tuple[str, OperatorRequest] = Depends(authorized_command),
    ) -> None:
        actor, _ = command
        await engine.cancel_all(actor)

    @app.post("/api/operator/kill")
    async def kill(
        command: tuple[str, OperatorRequest] = Depends(authorized_command),
    ) -> dict[str, Any]:
        actor, payload = command
        return (await engine.kill(payload.reason, actor)).model_dump(mode="json")

    @app.post("/api/operator/acknowledge")
    async def acknowledge(
        command: tuple[str, OperatorRequest] = Depends(authorized_command),
    ) -> dict[str, Any]:
        actor, payload = command
        return (await engine.acknowledge_kill(payload.reason, actor)).model_dump(mode="json")

    @app.get("/api/events")
    async def events(request: Request, _: str = Depends(authenticated)) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            last = ""
            while not await request.is_disconnected():
                payload = json.dumps(
                    {
                        "state": engine.state.model_dump(mode="json"),
                        "portfolio": engine.ledger.snapshot().model_dump(mode="json"),
                        "orders": [item.model_dump(mode="json") for item in engine.orders.all()],
                    }
                )
                if payload != last:
                    yield f"data: {payload}\n\n"
                    last = payload
                await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


def build_app() -> FastAPI:
    settings = AppSettings()
    settings.assert_safe()
    database = Database(settings.database.url, settings.database.echo)
    repository = DatabaseRepository(database)
    broker = IBKRBroker(settings.broker)
    engine = TradingEngine(
        settings,
        broker,
        repository,
        (MomentumStrategy(), MeanReversionStrategy()),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await database.create_schema()
        await engine.start()
        try:
            yield
        finally:
            await engine.close()
            await database.close()

    return create_app(engine, settings.operator_password.get_secret_value(), lifecycle=lifespan)


def main() -> None:
    uvicorn.run(build_app(), host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
