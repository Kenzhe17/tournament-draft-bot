"""Конфигурация сервиса Control Center (env-only, без секретов в коде)."""

from __future__ import annotations

import os


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


# Discord OAuth2 application (отдельное от бот-токена приложение
# или тот же bot с включённым OAuth2 — client_id/secret те же).
DISCORD_CLIENT_ID = _env("CONTROL_CENTER_DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = _env("CONTROL_CENTER_DISCORD_CLIENT_SECRET")
# Базовый URL этого сервиса (https://panel.example.com).
PUBLIC_BASE_URL = _env("CONTROL_CENTER_PUBLIC_URL", "http://localhost:8080")
REDIRECT_URI = _env(
    "CONTROL_CENTER_REDIRECT_URI",
    f"{PUBLIC_BASE_URL}/auth/callback",
)
# Секрет подписи сессий/CSRF-state. ОБЯЗАТЕЛЕН в production.
SESSION_SECRET = _env("CONTROL_CENTER_SESSION_SECRET")
SESSION_TTL_SECONDS = _env_int("CONTROL_CENTER_SESSION_TTL", 12 * 3600)
COOKIE_SECURE = _env("CONTROL_CENTER_COOKIE_SECURE", "auto").lower()

# Guild ID сервера, к которому привязана панель (0 = любой guild
# из ALLOWED_GUILD_IDS пользователя).
PANEL_GUILD_ID = _env_int("CONTROL_CENTER_GUILD_ID", 0)

# Набор Discord user ID с ролью owner панели (поверх BOT_OWNER_ID).
PANEL_OWNER_IDS = {
    int(x) for x in _env("CONTROL_CENTER_OWNER_IDS").split(",")
    if x.strip().isdigit()
}

# БД — та же PostgreSQL, что у бота.
DATABASE_URL = _env("DATABASE_URL")

# Discord API base (для тестов можно переопределить).
DISCORD_API_BASE = _env("DISCORD_API_BASE", "https://discord.com/api/v10")
OAUTH_AUTHORIZE_URL = _env(
    "DISCORD_OAUTH_AUTHORIZE_URL",
    "https://discord.com/oauth2/authorize",
)
OAUTH_TOKEN_URL = _env(
    "DISCORD_OAUTH_TOKEN_URL",
    f"{DISCORD_API_BASE}/oauth2/token",
)

DISCORD_SCOPES = "identify guilds"

# Health/readiness.
SERVICE_NAME = "control-center-api"
