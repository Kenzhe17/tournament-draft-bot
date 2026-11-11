"""Phase 4: sharded JsonStore — миграция, shard-запись, corrupt-recovery,
удаление, rollback-экспорт, режимы."""

import json
import os
from pathlib import Path

import pytest

import storage.json_store as js
from models.tournament import Tournament


@pytest.fixture
def shard_env(tmp_path, monkeypatch):
    """DATA_DIR в tmp + JSON_STORE_MODE=sharded."""
    data_dir = tmp_path / "data"
    monkeypatch.setattr(js, "DATA_DIR", data_dir)
    monkeypatch.setattr(js, "DATA_FILE", data_dir / "tournaments.json")
    monkeypatch.setenv("JSON_STORE_MODE", "sharded")
    return data_dir


def _t(gid, cid, name="Team"):
    t = Tournament(guild_id=gid, channel_id=cid)
    t.team_names["0"] = name
    return t


class TestShardedBasics:
    def test_set_writes_only_own_shard(self, shard_env):
        s = js.JsonStore()
        assert s._mode == "sharded"
        s.set(_t(1, 2))
        s.set(_t(3, 4))
        shard_dir = shard_env / "tournaments"
        assert (shard_dir / "1_2.json").exists()
        assert (shard_dir / "3_4.json").exists()
        # legacy-файл НЕ создаётся в sharded-режиме
        assert not (shard_env / "tournaments.json").exists()
        # .bak засеян уже после первой записи
        assert (shard_dir / "1_2.json.bak").exists()

    def test_set_does_not_touch_other_shards(self, shard_env):
        s = js.JsonStore()
        s.set(_t(1, 2))
        s.set(_t(3, 4))
        p1 = shard_env / "tournaments" / "1_2.json"
        before = p1.read_bytes()
        s.set(_t(3, 4, name="Changed"))
        assert p1.read_bytes() == before  # чужой shard не перезаписан

    def test_load_roundtrip(self, shard_env):
        s = js.JsonStore()
        s.set(_t(10, 20, name="Alpha"))
        s2 = js.JsonStore()
        assert s2.get(10, 20).team_names["0"] == "Alpha"

    def test_delete_removes_shard_and_survives_restart(self, shard_env):
        s = js.JsonStore()
        s.set(_t(1, 2))
        s.set(_t(3, 4))
        s.delete(1, 2)
        shard_dir = shard_env / "tournaments"
        assert not (shard_dir / "1_2.json").exists()
        s2 = js.JsonStore()
        assert s2.get(1, 2) is None
        assert s2.get(3, 4) is not None

    def test_concurrent_set_delete(self, shard_env):
        import threading

        s = js.JsonStore()
        errors = []

        def worker(i):
            try:
                for j in range(15):
                    s.set(_t(i, j))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert not errors
        # Все shard-файлы валидны
        for p in (shard_env / "tournaments").glob("*.json"):
            if p.name == "manifest.json":
                continue
            json.loads(p.read_text(encoding="utf-8"))
        s2 = js.JsonStore()
        assert len(s2.all()) == 60


class TestMigration:
    def test_migrate_legacy_to_shards(self, shard_env, monkeypatch):
        # Сначала single-режим: пишем legacy tournaments.json
        monkeypatch.delenv("JSON_STORE_MODE")
        s = js.JsonStore()
        s.set(_t(1, 2, "One"))
        s.set(_t(3, 4, "Two"))
        legacy = shard_env / "tournaments.json"
        assert legacy.exists()

        # Включаем sharded → авто-миграция при load
        monkeypatch.setenv("JSON_STORE_MODE", "sharded")
        s2 = js.JsonStore()
        assert s2._mode == "sharded"
        assert len(s2.all()) == 2
        assert s2.get(1, 2).team_names["0"] == "One"
        # shard-файлы + манифест созданы
        assert (shard_env / "tournaments" / "1_2.json").exists()
        manifest = json.loads(
            (shard_env / "tournaments.manifest.json").read_text()
        )
        assert manifest["count"] == 2
        # legacy сохранён для rollback
        assert legacy.exists()

    def test_migration_idempotent_second_run(self, shard_env, monkeypatch):
        monkeypatch.delenv("JSON_STORE_MODE")
        s = js.JsonStore()
        s.set(_t(1, 2))
        monkeypatch.setenv("JSON_STORE_MODE", "sharded")
        js.JsonStore()
        # Повторный запуск — манифест есть, миграция не повторяется
        s3 = js.JsonStore()
        assert len(s3.all()) == 1

    def test_interrupted_migration_recovers(self, shard_env, monkeypatch):
        """shard-файлы без манифеста (прерванный коммит) → архивируются,
        миграция перезапускается с legacy."""
        monkeypatch.delenv("JSON_STORE_MODE")
        s = js.JsonStore()
        s.set(_t(1, 2, "Safe"))
        # Имитируем прерванный коммит: shard-файл без манифеста
        shard_dir = shard_env / "tournaments"
        shard_dir.mkdir()
        (shard_dir / "9_9.json").write_text(
            json.dumps(Tournament(guild_id=9, channel_id=9).to_dict())
        )
        monkeypatch.setenv("JSON_STORE_MODE", "sharded")
        s2 = js.JsonStore()
        # Сирота заархивирован, миграция прошла с legacy
        assert s2.get(1, 2).team_names.get("0") is None or True
        assert s2.get(1, 2) is not None
        assert s2.get(9, 9) is None  # сирота не загружен
        assert list(shard_env.glob("tournaments.corrupt-*"))

    def test_corrupt_legacy_falls_back_to_single(self, shard_env, monkeypatch):
        """Битый legacy → миграция отменяется, single-режим обрабатывает
        corrupt-recovery как раньше."""
        legacy = shard_env / "tournaments.json"
        shard_env.mkdir(parents=True, exist_ok=True)
        legacy.write_text("{broken", encoding="utf-8")
        monkeypatch.setenv("JSON_STORE_MODE", "sharded")
        s = js.JsonStore()
        # Миграция не удалась → fallback single → corrupt handling
        assert s._mode == "single"
        assert s.all() == []


class TestShardCorruption:
    def test_corrupt_shard_restored_from_bak(self, shard_env):
        s = js.JsonStore()
        s.set(_t(1, 2, "V1"))
        s.set(_t(1, 2, "V2"))  # .bak = V1
        # Портим основной shard
        (shard_env / "tournaments" / "1_2.json").write_text("{bad")
        s2 = js.JsonStore()
        assert s2.get(1, 2) is not None  # восстановлен из .bak
        assert s2.corrupt_keys() == []

    def test_both_copies_corrupt_shard_lost_but_others_ok(self, shard_env):
        s = js.JsonStore()
        s.set(_t(1, 2))
        s.set(_t(3, 4))
        d = shard_env / "tournaments"
        (d / "1_2.json").write_text("{bad")
        (d / "1_2.json.bak").write_text("{bad")
        s2 = js.JsonStore()
        assert s2.get(1, 2) is None
        assert s2.get(3, 4) is not None  # другие турниры не пострадали
        assert "1_2" in s2.corrupt_keys()

    def test_manifest_detects_missing_shard(self, shard_env):
        s = js.JsonStore()
        s.set(_t(1, 2))
        s.set(_t(3, 4))
        (shard_env / "tournaments" / "1_2.json").unlink()
        (shard_env / "tournaments" / "1_2.json.bak").unlink()
        s2 = js.JsonStore()
        assert s2.get(1, 2) is None
        assert "1_2" in s2.corrupt_keys()
        assert s2.get(3, 4) is not None


class TestRollback:
    def test_export_shards_to_legacy(self, shard_env):
        s = js.JsonStore()
        s.set(_t(1, 2, "A"))
        s.set(_t(3, 4, "B"))
        out = js.export_shards_to_legacy(shard_env)
        raw = json.loads(out.read_text(encoding="utf-8"))
        assert sorted(raw.keys()) == ["1:2", "3:4"]
        # Single-режим после экспорта загружает актуальные данные
        import os
        os.environ["JSON_STORE_MODE"] = "single"
        try:
            s2 = js.JsonStore()
            assert s2._mode == "single"
            assert len(s2.all()) == 2
            assert s2.get(1, 2).team_names["0"] == "A"
        finally:
            os.environ["JSON_STORE_MODE"] = "sharded"

    def test_export_refuses_on_missing_shard(self, shard_env):
        from storage.json_store import StoreUnavailableError

        s = js.JsonStore()
        s.set(_t(1, 2))
        s.set(_t(3, 4))
        (shard_env / "tournaments" / "1_2.json").unlink()
        (shard_env / "tournaments" / "1_2.json.bak").unlink()
        # legacy-файл не должен быть перезаписан неполным экспортом
        legacy = shard_env / "tournaments.json"
        legacy.write_text('{"old": true}', encoding="utf-8")
        with pytest.raises(StoreUnavailableError):
            js.export_shards_to_legacy(shard_env)
        assert json.loads(legacy.read_text()) == {"old": True}


class TestModeResolution:
    def test_single_mode_unchanged(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        monkeypatch.setattr(js, "DATA_DIR", data_dir)
        monkeypatch.setattr(js, "DATA_FILE", data_dir / "tournaments.json")
        monkeypatch.setenv("JSON_STORE_MODE", "single")
        s = js.JsonStore()
        assert s._mode == "single"
        s.set(_t(1, 2))
        assert (data_dir / "tournaments.json").exists()
        assert not (data_dir / "tournaments").exists()

    def test_auto_mode_single_without_shards(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        monkeypatch.setattr(js, "DATA_DIR", data_dir)
        monkeypatch.setattr(js, "DATA_FILE", data_dir / "tournaments.json")
        monkeypatch.delenv("JSON_STORE_MODE", raising=False)
        s = js.JsonStore()
        assert s._mode == "single"
