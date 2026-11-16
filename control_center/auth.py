# ruff: noqa: B008
"""Discord OAuth2-аутентификация и сессии Control Center.

Поток:
  GET /auth/login     → redirect на Discord authorize (state + cookie)
  GET /auth/callback  → обмен code → user + guilds → RBAC → session cookie
  GET /auth/logout    → очистка cookie

Сессия — HMAC-подписанный токен в cookie `cc_session` (HttpOnly,
SameSite=Lax, Secure на https). Мутации дополнительно требуют
same-origin/CSRF-заголовок (verify_csrf dependency).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from control_center import config, rbac, security

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

SESSION_COOKIE = "cc_session"
STATE_COOKIE = "cc_oauth_state"
CSRF_HEADER = "x-cc-request"


class PanelUser:
    """Аутентифицированный пользователь панели."""

    def __init__(self, payload: dict[str, Any], perms: set[str]):
        self.id: int = int(payload["sub"])
        self.username: str = payload.get("username", "")
        self.avatar: str = payload.get("avatar", "")
        self.guild_id: int = int(payload.get("guild_id", 0))
        self.role: str | None = payload.get("role")
        self.permissions: set[str] = perms

    def can(self, perm: str) -> bool:
        return rbac.has_permission(self.permissions, perm)

    def require(self, perm: str) -> None:
        if not self.can(perm):
            raise HTTPException(
                status_code=403,
                detail=f"missing permission: {perm}",
            )


# ---------------- Discord API helpers ----------------

async def _discord_token(code: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            config.OAUTH_TOKEN_URL,
            data={
                "client_id": config.DISCORD_CLIENT_ID,
                "client_secret": config.DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code != 200:
        logger.warning("discord token exchange failed: %s", resp.status_code)
        raise HTTPException(status_code=502, detail="discord auth failed")
    return resp.json()


async def _discord_get(path: str, token: str) -> Any:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{config.DISCORD_API_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="discord api error")
    return resp.json()


def _find_panel_guild(guilds: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Гильдия, к которой привязана панель.

    CONTROL_CENTER_GUILD_ID > ALLOWED_GUILD_IDS бота > первая guild.
    """
    if config.PANEL_GUILD_ID:
        for g in guilds:
            if int(g.get("id", 0)) == config.PANEL_GUILD_ID:
                return g
        return None
    try:
        from config import ALLOWED_GUILD_IDS
        for g in guilds:
            if int(g.get("id", 0)) in ALLOWED_GUILD_IDS:
                return g
    except Exception as exc:  # noqa: BLE001 — config бота может быть недоступен
        logger.debug("ALLOWED_GUILD_IDS unavailable: %s", exc)
    return guilds[0] if guilds else None


def _is_bot_admin(user_id: int) -> bool:
    try:
        from config import ADMIN_USER_IDS, BOT_OWNER_ID
        return user_id == BOT_OWNER_ID or user_id in ADMIN_USER_IDS
    except Exception:  # noqa: BLE001
        return False


def _is_panel_owner(user_id: int) -> bool:
    try:
        from config import BOT_OWNER_ID
        if user_id == BOT_OWNER_ID:
            return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("BOT_OWNER_ID unavailable: %s", exc)
    return user_id in config.PANEL_OWNER_IDS


async def _resolve_access(
    user: dict[str, Any], guild: dict[str, Any] | None
) -> tuple[str | None, set[str]]:
    uid = int(user["id"])
    guild_id = int(guild["id"]) if guild else 0
    guild_owner = bool(guild and guild.get("owner"))
    try:
        perms = int(guild.get("permissions", "0")) if guild else 0
    except (TypeError, ValueError):
        perms = 0
    role = rbac.resolve_role(
        user_id=uid,
        guild_owner=guild_owner,
        guild_permissions=perms,
        is_bot_admin_id=_is_bot_admin(uid),
        is_panel_owner=_is_panel_owner(uid),
    )
    if role is None:
        # Нет Discord-уровня — проверяем явные гранты панели.
        try:
            grants = await _load_grants(guild_id, uid)
        except Exception:  # noqa: BLE001
            grants = set()
        if not grants:
            return None, set()
        role = "viewer"  # минимальная роль для входа
    perms_set = await rbac.effective_permissions(guild_id, uid, role)
    return role, perms_set


async def _load_grants(guild_id: int, user_id: int) -> set[str]:
    from storage import admin_config_store as store
    return await store.user_permissions(guild_id, user_id)


# ---------------- dependencies ----------------

async def current_user(request: Request) -> PanelUser:
    token = request.cookies.get(SESSION_COOKIE, "")
    payload = security.verify_session(token) if token else None
    if payload is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    # Свежие гранты из БД (недорогой запрос по PK).
    gid = int(payload.get("guild_id", 0))
    uid = int(payload["sub"])
    role = payload.get("role")
    try:
        perms = await rbac.effective_permissions(gid, uid, role)
    except Exception:  # noqa: BLE001 — БД недоступна: роль из сессии
        perms = rbac.permissions_for_role(role)
    return PanelUser(payload, perms)


def require_perm(perm: str):
    async def _dep(user: PanelUser = Depends(current_user)) -> PanelUser:
        user.require(perm)
        return user
    return _dep


async def verify_csrf(request: Request) -> None:
    """Защита мутаций: custom header или same-origin Origin/Referer.

    SameSite=Lax cookie уже блокирует cross-site POST с cookie,
    но явная проверка — второй рубеж (defence in depth).
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if request.headers.get(CSRF_HEADER):
        return
    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin:
        from urllib.parse import urlparse
        host = urlparse(origin).netloc
        if host and host == request.headers.get("host", ""):
            return
    raise HTTPException(status_code=403, detail="csrf check failed")


# ---------------- routes ----------------

@router.get("/login")
async def login(response: Response) -> RedirectResponse:
    if not config.DISCORD_CLIENT_ID:
        raise HTTPException(500, "OAuth not configured")
    nonce, digest = security.issue_state()
    params = (
        f"client_id={config.DISCORD_CLIENT_ID}"
        f"&redirect_uri={config.REDIRECT_URI}"
        f"&response_type=code&scope={config.DISCORD_SCOPES.replace(' ', '+')}"
        f"&state={nonce}"
    )
    resp = RedirectResponse(f"{config.OAUTH_AUTHORIZE_URL}?{params}")
    resp.set_cookie(
        STATE_COOKIE, digest, max_age=600, httponly=True,
        secure=security.cookie_secure(), samesite="lax", path="/auth",
    )
    return resp


@router.get("/callback")
async def callback(request: Request) -> RedirectResponse:
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    digest = request.cookies.get(STATE_COOKIE, "")
    if not code or not state or not digest:
        raise HTTPException(400, "missing oauth params")
    if not security.verify_state(state, digest):
        raise HTTPException(400, "invalid oauth state")

    token_data = await _discord_token(code)
    access_token = token_data["access_token"]
    user = await _discord_get("/users/@me", access_token)
    try:
        guilds = await _discord_get("/users/@me/guilds", access_token)
    except HTTPException:
        guilds = []

    guild = _find_panel_guild(guilds)
    role, _perms = await _resolve_access(user, guild)
    if role is None:
        return RedirectResponse(f"{config.PUBLIC_BASE_URL}/?error=forbidden")

    session = security.issue_session({
        "sub": str(user["id"]),
        "username": user.get("username", ""),
        "avatar": user.get("avatar", ""),
        "guild_id": int(guild["id"]) if guild else 0,
        "role": role,
    })
    resp = RedirectResponse(f"{config.PUBLIC_BASE_URL}/")
    resp.set_cookie(
        SESSION_COOKIE, session, max_age=config.SESSION_TTL_SECONDS,
        httponly=True, secure=security.cookie_secure(),
        samesite="lax", path="/",
    )
    resp.delete_cookie(STATE_COOKIE, path="/auth")
    return resp


@router.get("/logout")
async def logout() -> RedirectResponse:
    resp = RedirectResponse(f"{config.PUBLIC_BASE_URL}/")
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/me")
async def me(user: PanelUser = Depends(current_user)) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "username": user.username,
        "avatar": user.avatar,
        "guild_id": user.guild_id,
        "role": user.role,
        "permissions": sorted(user.permissions),
    }
