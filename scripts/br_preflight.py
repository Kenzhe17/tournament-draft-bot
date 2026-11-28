"""Pre-flight проверка состояния BR-хранилища (read-only).

Запускать внутри контейнера/на хосте production ДО деплоя и ПОСЛЕ:
    python scripts/br_preflight.py

Показывает активные и архивные BR турниры: фаза, формат, is_test,
отложенные публикации (publish_pending) — то, что важно для проверки
«сохранности существующих турниров» и поиска турниров, зависших
посередине публикации.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage.br_store import br_store  # noqa: E402


def main() -> None:
    active = br_store.all()
    history = br_store.history()
    print(f"Активных BR турниров: {len(active)}")
    for t in active:
        flags = []
        if t.is_test:
            flags.append("TEST")
        if t.publish_pending is not None:
            flags.append("PENDING_PUBLISH")
        if t.results_snapshot is not None:
            flags.append("SNAPSHOT")
        print(
            f"  id={t.id} guild={t.guild_id} channel={t.channel_id} "
            f"phase={t.phase.value} format={t.format.value} "
            f"slots={len(t.teams)} maps={t.map_count} msg={t.message_id} "
            f"{' '.join(flags)}"
        )
    print(f"Архивных BR турниров: {len(history)}")
    for t in history[-10:]:
        flags = "TEST" if t.is_test else ""
        print(
            f"  id={t.id} guild={t.guild_id} channel={t.channel_id} "
            f"phase={t.phase.value} format={t.format.value} {flags}"
        )
    pending = [t for t in active if t.publish_pending is not None]
    if pending:
        print(f"ВНИМАНИЕ: {len(pending)} турниров с отложенной публикацией — "
              "после деплоя resume_pending_publish доведёт их до конца.")
    if br_store.corrupt_count:
        print(f"КРИТИЧНО: {br_store.corrupt_count} повреждённых записей BR "
              "в карантине — восстановите данные до деплоя!")


if __name__ == "__main__":
    main()
