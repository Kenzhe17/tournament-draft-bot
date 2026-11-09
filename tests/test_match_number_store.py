import pytest

from storage.match_number_store import MatchNumberStore


@pytest.mark.asyncio
async def test_sequential_numbers(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.match_number_store.NUM_FILE", tmp_path / "mn.json")
    monkeypatch.setattr("storage.match_number_store.DATABASE_URL", "")
    store = MatchNumberStore()
    assert await store.get_or_assign("t1_final_0") == 1
    assert await store.get_or_assign("t1_qualifier_0") == 2
    assert await store.get_or_assign("t1_final_0") == 1  # стабильно


@pytest.mark.asyncio
async def test_announced_flag(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.match_number_store.NUM_FILE", tmp_path / "mn.json")
    monkeypatch.setattr("storage.match_number_store.DATABASE_URL", "")
    store = MatchNumberStore()
    await store.get_or_assign("k")
    assert not await store.is_announced("k")
    await store.mark_announced("k")
    assert await store.is_announced("k")
    await store.mark_announced("k")  # идемпотентно


@pytest.mark.asyncio
async def test_persistence_across_instances(tmp_path, monkeypatch):
    f = tmp_path / "mn.json"
    monkeypatch.setattr("storage.match_number_store.NUM_FILE", f)
    monkeypatch.setattr("storage.match_number_store.DATABASE_URL", "")
    s1 = MatchNumberStore()
    await s1.get_or_assign("a")
    await s1.get_or_assign("b")
    s2 = MatchNumberStore()
    assert await s2.get("a") == 1
    assert await s2.get_or_assign("c") == 3


@pytest.mark.asyncio
async def test_release_last_reuses_number(tmp_path, monkeypatch):
    """Отмена последнего выданного номера — следующий матч его получит."""
    monkeypatch.setattr("storage.match_number_store.NUM_FILE", tmp_path / "mn.json")
    monkeypatch.setattr("storage.match_number_store.DATABASE_URL", "")
    store = MatchNumberStore()
    assert await store.get_or_assign("m1") == 1
    assert await store.get_or_assign("m2") == 2
    assert await store.release("m2") is True
    assert await store.get_or_assign("m3") == 2


@pytest.mark.asyncio
async def test_release_middle_keeps_others(tmp_path, monkeypatch):
    """Отмена не-последнего номера не перенумеровывает чужие матчи."""
    monkeypatch.setattr("storage.match_number_store.NUM_FILE", tmp_path / "mn.json")
    monkeypatch.setattr("storage.match_number_store.DATABASE_URL", "")
    store = MatchNumberStore()
    await store.get_or_assign("m1")
    await store.get_or_assign("m2")
    await store.get_or_assign("m3")
    assert await store.release("m2") is True
    # m3 сохраняет номер 3 — его могли уже показать на карточке
    assert await store.get("m3") == 3
    # следующий матч получает следующий свободный номер
    assert await store.get_or_assign("m4") == 4


@pytest.mark.asyncio
async def test_release_announced_refused(tmp_path, monkeypatch):
    """Анонсированный матч нельзя «отменить» номером."""
    monkeypatch.setattr("storage.match_number_store.NUM_FILE", tmp_path / "mn.json")
    monkeypatch.setattr("storage.match_number_store.DATABASE_URL", "")
    store = MatchNumberStore()
    await store.get_or_assign("played")
    await store.mark_announced("played")
    assert await store.release("played") is False
    assert await store.get("played") == 1


@pytest.mark.asyncio
async def test_release_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.match_number_store.NUM_FILE", tmp_path / "mn.json")
    monkeypatch.setattr("storage.match_number_store.DATABASE_URL", "")
    store = MatchNumberStore()
    assert await store.release("nope") is False
