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
