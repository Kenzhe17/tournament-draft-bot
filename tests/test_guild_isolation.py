"""Phase: разделение конфигурации Production/Test.

Проверяем, что все серверные ID берутся из env, что без env
не остаётся production-дефолтов, и что механизмы изоляции
(ALLOWED_GUILD_IDS, guild_allowed, ADMIN_USER_IDS) работают.
"""

import importlib
import os

import pytest

import config


# env-ключи, которые тесты очищают/задают
_ID_ENV_KEYS = [
    "GUILD_ID", "ALLOWED_GUILD_IDS", "EXTRA_ADMIN_USER_IDS",
    "BOT_OWNER_ID", "ORG_ROLE_ID", "MANAGER_ROLE_ID",
    "ORG_1_ROLE_ID", "ORG_2_ROLE_ID", "ORG_3_ROLE_ID", "ORG_ROLE_MAP",
    "TOURNAMENT_CHANNELS", "MATCH_FOUND_CHANNEL_ID",
    "MM_MATCH_CHANNEL_POOL", "NOTIFICATION_ROLE_ID",
    "NOTIFICATION_CHANNEL_ID", "MOD_LOG_WEBHOOK_URL",
    "MOD_LOG_CHANNEL_ID", "MUTE_ROLE_ID", "BR_ROLE_ID",
    "BR_ROOM_CHANNEL_ID", "BR_DATA_CHANNEL_ID", "BR_TEAM_CHANNEL_ID",
    "OCR_RESULT_CHANNEL_ID", "OCR_STATS_CHANNEL_ID",
    "OCR_ADMIN_ROLE_ID", "ULT_ROLE_ID", "ULT_ROOM_CHANNEL_ID",
    "WELCOME_CHANNEL_ID", "PET_HUNGER_CHANNEL_ID",
    "ROOM_DATA_CHANNEL_ID", "GAMES_CHANNEL_ID", "INFO_CHANNEL_ID",
    "REP_LOG_CHANNEL_ID", "GIF_TRIGGER_USER_IDS",
]


@pytest.fixture
def clean_env(monkeypatch):
    """Пустое окружение для config + reload модуля."""
    for k in _ID_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    importlib.reload(config)
    yield config
    importlib.reload(config)


class TestNoProdDefaults:
    def test_all_ids_zero_without_env(self, clean_env):
        cfg = clean_env
        for name in (
            "GUILD_ID", "ORG_ROLE_ID", "MANAGER_ROLE_ID",
            "ORG_1_ROLE_ID", "ORG_2_ROLE_ID", "ORG_3_ROLE_ID",
            "NOTIFICATION_ROLE_ID", "NOTIFICATION_CHANNEL_ID",
            "MOD_LOG_CHANNEL_ID", "MUTE_ROLE_ID", "BR_ROLE_ID",
            "BR_ROOM_CHANNEL_ID", "BR_DATA_CHANNEL_ID",
            "BR_TEAM_CHANNEL_ID", "OCR_RESULT_CHANNEL_ID",
            "OCR_STATS_CHANNEL_ID", "OCR_ADMIN_ROLE_ID",
            "ULT_ROLE_ID", "ULT_ROOM_CHANNEL_ID",
            "WELCOME_CHANNEL_ID", "PET_HUNGER_CHANNEL_ID",
            "ROOM_DATA_CHANNEL_ID", "GAMES_CHANNEL_ID",
            "INFO_CHANNEL_ID", "REP_LOG_CHANNEL_ID",
        ):
            assert getattr(cfg, name) == 0, f"{name} имеет дефолт"

    def test_collections_empty_without_env(self, clean_env):
        cfg = clean_env
        assert cfg.ORG_ROLE_MAP == {}
        assert cfg.TOURNAMENT_CHANNELS == []
        assert cfg.MM_MATCH_CHANNEL_POOL == []
        assert cfg.MATCH_FOUND_CHANNEL_ID == 0
        assert cfg.ADMIN_USER_IDS == set()
        assert cfg.ALLOWED_GUILD_IDS == set()

    def test_webhook_secret_not_in_source(self, clean_env):
        """Webhook URL не должен иметь дефолта — только env."""
        assert clean_env.MOD_LOG_WEBHOOK_URL == ""


class TestEnvParsing:
    def test_guild_and_allowed(self, monkeypatch, clean_env):
        monkeypatch.setenv("GUILD_ID", "111")
        monkeypatch.setenv("ALLOWED_GUILD_IDS", "111,222")
        importlib.reload(config)
        try:
            assert config.GUILD_ID == 111
            assert config.ALLOWED_GUILD_IDS == {111, 222}
            assert config.guild_allowed(111)
            assert config.guild_allowed(222)
            assert not config.guild_allowed(999)
            assert not config.guild_allowed(None)
        finally:
            importlib.reload(config)

    def test_allowed_empty_means_all(self, clean_env):
        assert clean_env.guild_allowed(12345)
        assert clean_env.guild_allowed(None)

    def test_admin_user_ids_union(self, monkeypatch, clean_env):
        monkeypatch.setenv("BOT_OWNER_ID", "100")
        monkeypatch.setenv("EXTRA_ADMIN_USER_IDS", "200,300")
        importlib.reload(config)
        try:
            assert config.ADMIN_USER_IDS == {100, 200, 300}
        finally:
            importlib.reload(config)

    def test_org_role_map_parse(self, monkeypatch, clean_env):
        monkeypatch.setenv(
            "ORG_ROLE_MAP", "111:222, 333 : 444 ,bad,bad2:x"
        )
        importlib.reload(config)
        try:
            assert config.ORG_ROLE_MAP == {111: 222, 333: 444}
        finally:
            importlib.reload(config)

    def test_match_found_fallback_to_first_tournament_channel(
        self, monkeypatch, clean_env
    ):
        monkeypatch.setenv("TOURNAMENT_CHANNELS", "10,20,30")
        importlib.reload(config)
        try:
            assert config.MATCH_FOUND_CHANNEL_ID == 10
            monkeypatch.setenv("MATCH_FOUND_CHANNEL_ID", "99")
            importlib.reload(config)
            assert config.MATCH_FOUND_CHANNEL_ID == 99
        finally:
            importlib.reload(config)


class TestIsolationBehavior:
    def test_permissions_use_admin_user_ids(self):
        """utils.permissions не содержит hardcoded user ID —
        проверяем через ADMIN_USER_IDS из config."""
        import utils.permissions as perm
        import inspect
        src = inspect.getsource(perm)
        assert "1032544122600423427" not in src
        assert "ADMIN_USER_IDS" in src

    def test_moderation_gif_users_from_config(self):
        import cogs.moderation as mod
        import inspect
        src = inspect.getsource(mod)
        assert "1032544122600423427" not in src
        assert "582145013358657549" not in src
        assert "575937439147753472" not in src
        assert "ADMIN_USER_IDS" in src

    def test_no_hardcoded_channel_ids_in_runtime_code(self):
        """Smoke: ключевые модули не содержат prod channel ID."""
        prod_ids = {
            "1549809898643001484", "1563149967303118949",
            "1563149951033151609", "1200125075156910181",
            "1569098034325889144", "1568608461250367569",
            "1563195265395982466", "1561683314480255057",
            "1567585136957063229", "1551167853741219880",
            "1561707634938351728", "1196165488565289012",
        }
        files = [
            "bot.py", "cogs/mm.py", "cogs/ocr_upload.py",
            "cogs/coin_drop.py", "cogs/ultimatum.py",
            "views/br_view.py", "views/room_modal.py",
            "views/games_view.py", "views/ocr_match_view.py",
            "views/ultimatum_room_modal.py", "ultimatum/tournament.py",
            "services/welcome_card.py", "storage/pet_store.py",
            "utils/permissions.py", "cogs/moderation.py",
        ]
        for f in files:
            src = open(f, encoding="utf8").read()
            found = prod_ids & set(
                __import__("re").findall(r"\d{17,20}", src)
            )
            assert not found, f"{f}: hardcoded IDs {found}"
