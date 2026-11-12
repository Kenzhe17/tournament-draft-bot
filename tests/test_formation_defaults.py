"""Режим формирования по умолчанию — DRAFT.

- dataclass default;
- from_dict fallback при отсутствии ключа;
- явный выбор режима сохраняется;
- legacy-режимы загружаются без изменений.
"""

from models.tournament import FormationMode, Tournament


def _minimal(**kw):
    base = {"guild_id": 1, "channel_id": 2}
    base.update(kw)
    return base


class TestFormationDefaults:
    def test_dataclass_default_is_draft(self):
        t = Tournament(**_minimal())
        assert t.formation_mode == FormationMode.DRAFT

    def test_from_dict_missing_key_keeps_legacy_elo(self):
        """Старые сохранения без formation_mode — турниры эпохи
        ELO: их режим НЕ меняется (обратная совместимость)."""
        t = Tournament.from_dict(_minimal())
        assert t.formation_mode == FormationMode.ELO

    def test_explicit_balanced_sr_preserved(self):
        t = Tournament.from_dict(
            _minimal(formation_mode="balanced-sr")
        )
        assert t.formation_mode == FormationMode.BALANCED_SR

    def test_explicit_draft_preserved(self):
        t = Tournament.from_dict(_minimal(formation_mode="draft"))
        assert t.formation_mode == FormationMode.DRAFT

    def test_legacy_modes_load_unchanged(self):
        for value in ("elo", "skill", "random", "random-auto"):
            t = Tournament.from_dict(
                _minimal(formation_mode=value)
            )
            assert t.formation_mode.value == value

    def test_to_dict_roundtrip(self):
        t = Tournament.from_dict(
            _minimal(formation_mode="balanced-sr")
        )
        data = t.to_dict()
        assert data["formation_mode"] == "balanced-sr"
        t2 = Tournament.from_dict(data)
        assert t2.formation_mode == FormationMode.BALANCED_SR
