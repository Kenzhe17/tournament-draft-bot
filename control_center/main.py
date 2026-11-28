"""FastAPI-приложение Control Center.

Запуск: uvicorn control_center.main:app --host 0.0.0.0 --port 8080

Отдельный Railway-сервис; использует ту же DATABASE_URL,
что и бот. Discord OAuth2 настраивается через env
(control_center/config.py).
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from control_center import config
from control_center.auth import router as auth_router
from control_center.routers import (
    admin_modules,
    blackjack_api,
    games_api,
    ops_api,
    players_api,
    settings_api,
    shop_api,
    system_api,
)

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(
        title="r!z3 Control Center API",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    # Панель и API могут быть на разных origin (Vite dev / Railway).
    # SameSite=Lax + CSRF-dependency остаются основной защитой;
    # CORS открываем только для credentialed-запросов с явных origin.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[config.PUBLIC_BASE_URL],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CC-Request"],
    )

    app.include_router(auth_router)
    app.include_router(system_api.router)
    app.include_router(settings_api.router)
    app.include_router(shop_api.router)
    app.include_router(games_api.router)
    app.include_router(ops_api.router)
    app.include_router(players_api.router)
    app.include_router(admin_modules.router)
    app.include_router(blackjack_api.router)

    missing = config.missing_required()
    if missing:
        logger.error(
            "Control Center: отсутствуют обязательные переменные: %s",
            ", ".join(missing),
        )
    else:
        logger.info("Control Center: все обязательные переменные заданы")

    # React-сборка панели (panel/dist). Если каталог отсутствует
    # (dev-окружение без frontend build), API продолжает работать —
    # статика просто не монтируется.
    _panel_dist = Path(__file__).resolve().parent.parent / "panel" / "dist"
    if _panel_dist.is_dir():
        app.mount(
            "/",
            StaticFiles(directory=str(_panel_dist), html=True),
            name="panel",
        )
    else:
        @app.get("/")
        async def root() -> dict:
            return {"service": config.SERVICE_NAME, "docs": "/api/docs"}

    return app


app = create_app()
