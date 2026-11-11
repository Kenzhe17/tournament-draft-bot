"""Тесты атомарной записи и восстановления JSON-хранилища."""

import json
import threading
from pathlib import Path

import pytest

import storage.json_store as js
from models.tournament import Tournament


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    """JsonStore с DATA_DIR/DATA_FILE в tmp_path."""
    data_dir = tmp_path / "data"
    data_file = data_dir / "tournaments.json"
    monkeypatch.setattr(js, "DATA_DIR", data_dir)
    monkeypatch.setattr(js, "DATA_FILE", data_file)
    return data_dir, data_file


def _new_store():
    return js.JsonStore()


class TestAtomicWrite:
    def test_save_creates_valid_json(self, store_env):
        data_dir, data_file = store_env
        s = _new_store()
        s.set(Tournament(guild_id=1, channel_id=2))
        assert data_file.exists()
        raw = json.loads(data_file.read_text(encoding="utf-8"))
        assert "1:2" in raw
        # .tmp не остаётся
        assert not (data_dir / "tournaments.json.tmp").exists()

    def test_backup_created_on_second_save(self, store_env):
        data_dir, data_file = store_env
        s = _new_store()
        s.set(Tournament(guild_id=1, channel_id=2))
        t2 = Tournament(guild_id=3, channel_id=4)
        s.set(t2)
        bak = data_dir / "tournaments.json.bak"
        assert bak.exists()
        bak_raw = json.loads(bak.read_text(encoding="utf-8"))
        # bak = предыдущая версия (только первый турнир)
        assert "1:2" in bak_raw and "3:4" not in bak_raw

    def test_load_roundtrip(self, store_env):
        data_dir, data_file = store_env
        s = _new_store()
        t = Tournament(guild_id=10, channel_id=20)
        t.team_names[0] = "TeamA"
        s.set(t)
        s2 = _new_store()
        loaded = s2.get(10, 20)
        assert loaded is not None
        assert loaded.team_names.get("0") == "TeamA"

    def test_concurrent_saves_keep_valid_json(self, store_env):
        data_dir, data_file = store_env
        s = _new_store()
        errors = []

        def worker(i):
            try:
                for j in range(20):
                    s.set(Tournament(guild_id=i, channel_id=j))
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert not errors
        # файл всегда валидный JSON
        raw = json.loads(data_file.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)

    def test_serialize_failure_keeps_old_file(self, store_env):
        data_dir, data_file = store_env
        s = _new_store()
        s.set(Tournament(guild_id=1, channel_id=2))
        good = data_file.read_text(encoding="utf-8")

        class BadTournament:
            guild_id = 9
            channel_id = 9

            def to_dict(self):
                return {"bad": object()}  # не сериализуется

        # json.dumps упадёт на object() внутри to_dict → save()
        # выбрасывает StoreUnavailableError, старый файл не тронут
        from storage.json_store import StoreUnavailableError
        s._tournaments[(9, 9)] = BadTournament()
        with pytest.raises(StoreUnavailableError):
            s.save()
        assert data_file.read_text(encoding="utf-8") == good


class TestCorruptionRecovery:
    def test_corrupt_main_restored_from_bak(self, store_env):
        data_dir, data_file = store_env
        s = _new_store()
        s.set(Tournament(guild_id=1, channel_id=2))
        s.set(Tournament(guild_id=3, channel_id=4))  # создаёт .bak

        # Имитируем повреждение основного файла
        data_file.write_text('{"1:2": {broken json', encoding="utf-8")

        s2 = _new_store()
        # Данные восстановлены из .bak (версия с 1:2, возможно без 3:4)
        loaded = s2.get(1, 2)
        assert loaded is not None
        # Битый файл отложен в *.corrupt-*
        corrupts = list(data_dir.glob("tournaments.json.corrupt-*"))
        assert len(corrupts) == 1
        # Основной файл снова валиден
        json.loads(data_file.read_text(encoding="utf-8"))

    def test_corrupt_without_bak_starts_empty_but_preserves(self, store_env):
        data_dir, data_file = store_env
        data_dir.mkdir(parents=True, exist_ok=True)
        data_file.write_text("not json at all", encoding="utf-8")

        s = _new_store()
        assert s.all() == []
        # Битый файл сохранён для диагностики
        assert list(data_dir.glob("tournaments.json.corrupt-*"))

    def test_bak_also_corrupt_starts_empty(self, store_env):
        data_dir, data_file = store_env
        data_dir.mkdir(parents=True, exist_ok=True)
        data_file.write_text("{bad", encoding="utf-8")
        (data_dir / "tournaments.json.bak").write_text("{bad2", encoding="utf-8")

        s = _new_store()
        assert s.all() == []
        assert list(data_dir.glob("tournaments.json.corrupt-*"))


class TestUnifiedDataDir:
    def test_all_stores_use_config_data_dir(self):
        """Все JSON-сторы используют config.DATA_DIR (абсолютный путь)."""
        import config
        from pathlib import Path

        modules = [
            "storage.bet_store", "storage.case_store",
            "storage.match_history_store", "storage.player_stats_store",
            "storage.question_store", "storage.shop_store",
            "storage.user_balance_store", "storage.wardrobe_store",
            "storage.json_store", "storage.mm_search_store",
            "storage.team_store", "storage.ready_check_store",
            "storage.organizer_store", "storage.rep_store",
            "storage.profile_cooldowns", "storage.br_store",
            "storage.match_number_store",
        ]
        import importlib
        for name in modules:
            mod = importlib.import_module(name)
            data_dir = getattr(mod, "DATA_DIR", None)
            assert data_dir is not None, f"{name}: нет DATA_DIR"
            assert Path(data_dir) == Path(config.DATA_DIR), (
                f"{name}: DATA_DIR={data_dir} != config.DATA_DIR"
            )
            assert Path(data_dir).is_absolute(), f"{name}: путь не абсолютный"

    def test_data_dir_env_respected(self, monkeypatch):
        """DATA_DIR env задаёт каталог (config читает env при импорте)."""
        import os
        import config

        # config.DATA_DIR уже вычислен при импорте — проверяем формулу
        expected = Path(
            os.getenv("DATA_DIR", str(config.BASE_DIR / "data"))
        )
        assert Path(config.DATA_DIR) == expected


class TestSettingsAtomic:
    def test_settings_roundtrip(self, store_env):
        data_dir, _ = store_env
        s = _new_store()
        s.set_settings({"key": "value", "n": 1})
        assert s.get_settings() == {"key": "value", "n": 1}
        assert (data_dir / "settings.json").exists()

    def test_settings_corrupt_returns_empty(self, store_env):
        data_dir, _ = store_env
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "settings.json").write_text("{bad", encoding="utf-8")
        s = _new_store()
        assert s.get_settings() == {}


class TestUnhealthyNoOverwrite:
    def test_corrupt_no_bak_is_unhealthy_and_blocks_save(self, store_env):
        from storage.json_store import StoreUnavailableError

        data_dir, data_file = store_env
        data_dir.mkdir(parents=True, exist_ok=True)
        data_file.write_text("{totally broken", encoding="utf-8")

        s = _new_store()
        assert s.is_healthy() is False
        assert s.all() == []
        # set/save выбрасывают явную ошибку — пустое состояние
        # не может затереть повреждённые данные
        with pytest.raises(StoreUnavailableError):
            s.set(Tournament(guild_id=1, channel_id=2))
        with pytest.raises(StoreUnavailableError):
            s.save()
        assert not data_file.exists()
        # оригинал сохранён для диагностики
        assert list(data_dir.glob("tournaments.json.corrupt-*"))

    def test_unhealthy_self_recovery_after_admin_fix(self, store_env):
        """Админ подменил битый файл валидным — стор восстанавливается."""
        data_dir, data_file = store_env
        data_dir.mkdir(parents=True, exist_ok=True)
        data_file.write_text("{broken", encoding="utf-8")

        s = _new_store()
        assert s.is_healthy() is False

        # Админ восстанавливает файл вручную
        data_file.write_text("{}", encoding="utf-8")
        s.set(Tournament(guild_id=1, channel_id=2))
        assert s.is_healthy() is True
        assert s.get(1, 2) is not None
        assert json.loads(data_file.read_text())

    def test_first_run_no_file_is_healthy(self, store_env):
        data_dir, data_file = store_env
        s = _new_store()
        assert s.is_healthy() is True
        assert s.all() == []
        s.set(Tournament(guild_id=1, channel_id=2))
        assert data_file.exists()

    def test_corrupt_with_valid_bak_recovers_and_healthy(self, store_env):
        data_dir, data_file = store_env
        s = _new_store()
        s.set(Tournament(guild_id=1, channel_id=2))
        s.set(Tournament(guild_id=3, channel_id=4))
        data_file.write_text("{broken", encoding="utf-8")
        s2 = _new_store()
        assert s2.is_healthy() is True
        assert s2.get(1, 2) is not None


class TestAtomicBackup:
    def test_bak_not_overwritten_by_corrupt_current(self, store_env):
        """Если текущий файл битый — .bak не обновляется мусором."""
        from storage.json_atomic import rotate_backup

        data_dir, data_file = store_env
        data_dir.mkdir(parents=True, exist_ok=True)
        good = {"a": 1}
        data_file.write_text(json.dumps(good), encoding="utf-8")
        assert rotate_backup(data_file) is True
        bak = data_dir / "tournaments.json.bak"
        assert json.loads(bak.read_text()) == good

        # Текущий файл портится — rotate_backup не должен его копировать
        data_file.write_text("{broken", encoding="utf-8")
        assert rotate_backup(data_file) is False
        assert json.loads(bak.read_text()) == good  # bak нетронут

    def test_atomic_write_text_leaves_no_tmp(self, store_env):
        from storage.json_atomic import atomic_write_text

        data_dir, _ = store_env
        data_dir.mkdir(parents=True, exist_ok=True)
        target = data_dir / "x.json"
        atomic_write_text(target, '{"k": 1}')
        assert json.loads(target.read_text()) == {"k": 1}
        assert not (data_dir / "x.json.tmp").exists()


class TestOtherStoresAtomic:
    @pytest.mark.asyncio
    async def test_user_balance_save_atomic(self, store_env, monkeypatch, tmp_path):
        """user_balance_store пишет JSON атомарно (fallback-режим)."""
        import storage.user_balance_store as ubs

        bal_file = tmp_path / "ub.json"
        decay_file = tmp_path / "decay.json"
        monkeypatch.setattr(ubs, "BALANCE_FILE", bal_file)
        monkeypatch.setattr(ubs, "DECAY_FILE", decay_file)
        monkeypatch.setattr(ubs.decay_tracker, "should_apply_decay",
                            lambda g, u: False)
        store = ubs.UserBalanceStore()
        store._use_db = False
        store._balances = {}
        new_bal = await store.add_balance(1, 2, 50)
        assert new_bal == 150
        assert json.loads(bal_file.read_text()) != {}
        assert not (tmp_path / "ub.json.tmp").exists()
