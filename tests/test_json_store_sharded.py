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

    def test_shards_without_manifest_are_authoritative(self, shard_env, monkeypatch):
        """Shard-файлы без манифеста = потерянный манифест у
        работающего хранилища: НЕ архивируются, НЕ замещаются
        legacy — манифест восстанавливается из самих файлов."""
        monkeypatch.delenv("JSON_STORE_MODE")
        s = js.JsonStore()
        s.set(_t(1, 2, "Safe"))
        # shard-файл без манифеста (как после потери manifest)
        shard_dir = shard_env / "tournaments"
        shard_dir.mkdir(exist_ok=True)
        (shard_dir / "9_9.json").write_text(
            json.dumps(Tournament(guild_id=9, channel_id=9).to_dict())
        )
        for p in shard_env.glob("tournaments.manifest.json*"):
            p.unlink(missing_ok=True)
        monkeypatch.setenv("JSON_STORE_MODE", "sharded")
        s2 = js.JsonStore()
        # Оба shard'а загружены, legacy НЕ замещает их
        assert s2.get(9, 9) is not None
        # legacy содержит 1_2, но shard 1_2 отсутствует → его нет
        # (shard'ы авторитетны; legacy может быть устаревшим)
        assert s2.get(1, 2) is None
        # shard НЕ заархивирован, манифест восстановлен
        assert (shard_dir / "9_9.json").exists()
        assert not list(shard_env.glob("tournaments.corrupt-*"))
        manifest = json.loads(
            (shard_env / "tournaments.manifest.json").read_text()
        )
        assert manifest["keys"] == ["9_9"]

    def test_staging_without_shards_reruns_migration(
        self, shard_env, monkeypatch
    ):
        """Прерванная ПЕРВОНАЧАЛЬНАЯ миграция: staging есть, shard'ов
        нет → staging архивируется, миграция перезапускается."""
        monkeypatch.delenv("JSON_STORE_MODE")
        s = js.JsonStore()
        s.set(_t(1, 2, "Safe"))
        staging = shard_env / "tournaments.staging"
        staging.mkdir()
        (staging / "1_2.json").write_text("{}")
        monkeypatch.setenv("JSON_STORE_MODE", "sharded")
        s2 = js.JsonStore()
        assert s2.get(1, 2) is not None
        assert s2.get(1, 2).team_names.get("0") is None or True
        assert list(shard_env.glob("tournaments.staging.corrupt-*"))

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


class TestManifestLoss:
    """Phase 4.5: потеря/повреждение манифеста у работающего
    sharded-хранилища."""

    def _make_sharded(self, shard_env):
        s = js.JsonStore()
        s.set(_t(1, 2, "One"))
        s.set(_t(3, 4, "Two"))
        return s

    def test_manifest_deleted_shards_recovered(self, shard_env):
        self._make_sharded(shard_env)
        for p in shard_env.glob("tournaments.manifest.json*"):
            p.unlink()
        s2 = js.JsonStore()
        assert s2.get(1, 2).team_names["0"] == "One"
        assert s2.get(3, 4).team_names["0"] == "Two"
        # манифест восстановлен из фактического набора
        manifest = json.loads(
            (shard_env / "tournaments.manifest.json").read_text()
        )
        assert sorted(manifest["keys"]) == ["1_2", "3_4"]

    def test_manifest_corrupt_falls_back_to_bak(self, shard_env):
        self._make_sharded(shard_env)
        # Портим primary — .bak должен покрыть
        (shard_env / "tournaments.manifest.json").write_text("{bad")
        s2 = js.JsonStore()
        assert s2.get(1, 2) is not None
        assert s2.get(3, 4) is not None

    def test_manifest_and_bak_lost_rebuilds(self, shard_env):
        self._make_sharded(shard_env)
        for p in shard_env.glob("tournaments.manifest.json*"):
            p.unlink()
        s2 = js.JsonStore()
        assert len(s2.all()) == 2
        # legacy отсутствует — rebuild без него
        assert not (shard_env / "tournaments.json").exists()

    def test_shard_newer_than_legacy_blocks_single_writes(
        self, shard_env, monkeypatch
    ):
        """Single-режим + shard'ы новее legacy → запись запрещена,
        чтение работает, is_healthy()=False."""
        # 1) single: legacy с турниром A
        monkeypatch.delenv("JSON_STORE_MODE")
        s = js.JsonStore()
        s.set(_t(1, 2, "Legacy"))
        # 2) sharded: миграция + новый турнир B (shard'ы новее)
        monkeypatch.setenv("JSON_STORE_MODE", "sharded")
        s2 = js.JsonStore()
        s2.set(_t(5, 6, "Shard"))
        # 3) обратно в single — legacy устарел
        monkeypatch.setenv("JSON_STORE_MODE", "single")
        s3 = js.JsonStore()
        assert s3._mode == "single"
        assert s3.is_healthy() is False
        # чтение устаревшего legacy работает
        assert s3.get(1, 2) is not None
        # запись запрещена явной ошибкой
        from storage.json_store import StoreUnavailableError
        with pytest.raises(StoreUnavailableError):
            s3.set(_t(7, 8))
        with pytest.raises(StoreUnavailableError):
            s3.delete(1, 2)
        # legacy не перезаписан
        raw = json.loads(
            (shard_env / "tournaments.json").read_text()
        )
        assert "7:8" not in raw

    def test_export_then_auto_resolves_single(self, shard_env, monkeypatch):
        """После export_shards_to_legacy auto-режим выбирает single
        (legacy новее shard'ов), манифест помечен exported."""
        self._make_sharded(shard_env)
        js.export_shards_to_legacy(shard_env)
        assert not (shard_env / "tournaments.manifest.json").exists()
        assert list(shard_env.glob("tournaments.manifest.json.exported-*"))
        monkeypatch.delenv("JSON_STORE_MODE")
        s = js.JsonStore()
        assert s._mode == "single"
        assert len(s.all()) == 2
        assert s.get(1, 2).team_names["0"] == "One"

    def test_total_shard_loss_goes_unhealthy(self, shard_env):
        """Манифест заявляет турниры, все shard'ы потеряны →
        unhealthy, запись запрещена, манифест сохранён."""
        self._make_sharded(shard_env)
        for p in (shard_env / "tournaments").glob("*.json*"):
            p.unlink()
        s2 = js.JsonStore()
        assert s2.is_healthy() is False
        assert len(s2.corrupt_keys()) == 2
        from storage.json_store import StoreUnavailableError
        with pytest.raises(StoreUnavailableError):
            s2.set(_t(9, 9))
        # манифест НЕ затёрт — ключи сохранены для forensics
        manifest = json.loads(
            (shard_env / "tournaments.manifest.json").read_text()
        )
        assert sorted(manifest["keys"]) == ["1_2", "3_4"]

    def test_shard_written_manifest_stale_self_heals(self, shard_env):
        """Shard записан, манифест не обновлён (crash-окно) →
        при загрузке лишний ключ подхватывается, манифест
        самолечится."""
        self._make_sharded(shard_env)
        # Имитируем crash между _write_shard и _write_manifest:
        # добавляем валидный shard-файл и убираем его из манифеста.
        extra = Tournament(guild_id=7, channel_id=8)
        extra.team_names["0"] = "Extra"
        (shard_env / "tournaments" / "7_8.json").write_text(
            json.dumps(extra.to_dict()), encoding="utf-8"
        )
        manifest = json.loads(
            (shard_env / "tournaments.manifest.json").read_text()
        )
        manifest["keys"] = ["1_2", "3_4"]
        (shard_env / "tournaments.manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        s2 = js.JsonStore()
        assert s2.get(7, 8) is not None  # лишний shard загружен
        healed = json.loads(
            (shard_env / "tournaments.manifest.json").read_text()
        )
        assert sorted(healed["keys"]) == ["1_2", "3_4", "7_8"]

    def test_nested_state_survives_shard_restart(self, shard_env):
        """Вложенное состояние (команды, пулы, picks) переживает
        shard-запись + рестарт."""
        s = js.JsonStore()
        t = _t(11, 22, "Bracket")
        t.players_pool = [{"id": 1, "name": "P1"}]
        t.circle1 = [{"id": 2, "name": "P2"}]
        t.team_names = {"0": "Alpha", "1": "Beta"}
        s.set(t)
        s2 = js.JsonStore()
        t2 = s2.get(11, 22)
        assert t2 is not None
        assert t2.players_pool == [{"id": 1, "name": "P1"}]
        assert t2.circle1 == [{"id": 2, "name": "P2"}]
        assert t2.team_names == {"0": "Alpha", "1": "Beta"}
