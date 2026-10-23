"""Storage for channel organizers."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from config import DATA_DIR

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

ORGANIZER_DATA_FILE = DATA_DIR / "organizers.json"
MAX_ORGANIZERS_PER_CHANNEL = 3


class ChannelOrganizers:
    """Organizers for a channel."""

    def __init__(self):
        self.organizer_id: int = 0  # Main organizer
        self.additional_organizers: list[int] = []  # Additional organizers
        self.tournament_id: str = ""  # Current tournament ID in this channel
        self.message_id: int = 0  # Message ID for organizer management embed

    def to_dict(self) -> dict:
        return {
            "organizer_id": self.organizer_id,
            "additional_organizers": self.additional_organizers,
            "tournament_id": self.tournament_id,
            "message_id": self.message_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ChannelOrganizers":
        org = cls()
        org.organizer_id = data.get("organizer_id", 0)
        org.additional_organizers = data.get("additional_organizers", [])
        org.tournament_id = data.get("tournament_id", "")
        org.message_id = data.get("message_id", 0)
        return org

    def all_organizers(self) -> list[int]:
        """Get all organizers (main + additional)."""
        organizers = [self.organizer_id] if self.organizer_id else []
        organizers.extend(self.additional_organizers)
        return organizers

    def is_organizer(self, user_id: int) -> bool:
        """Check if user is an organizer of this channel."""
        return user_id in self.all_organizers()

    def add_organizer(self, user_id: int) -> bool:
        """Add an additional organizer.

        Returns:
            True if successful, False if limit reached or already exists
        """
        if user_id in self.all_organizers():
            return False

        if len(self.all_organizers()) >= MAX_ORGANIZERS_PER_CHANNEL:
            return False

        self.additional_organizers.append(user_id)
        return True

    def remove_organizer(self, user_id: int) -> bool:
        """Remove an organizer.

        Returns:
            True if successful, False if not found
        """
        if user_id == self.organizer_id:
            self.organizer_id = 0
            return True

        if user_id in self.additional_organizers:
            self.additional_organizers.remove(user_id)
            return True

        return False

    def transfer_organizer(self, new_organizer_id: int) -> bool:
        """Transfer main organizer role to another user.

        Returns:
            True if successful, False if user not found
        """
        if new_organizer_id not in self.all_organizers():
            return False

        # Swap: current main becomes additional, new user becomes main
        if self.organizer_id and self.organizer_id != new_organizer_id:
            if self.organizer_id not in self.additional_organizers:
                self.additional_organizers.append(self.organizer_id)

        self.organizer_id = new_organizer_id
        if new_organizer_id in self.additional_organizers:
            self.additional_organizers.remove(new_organizer_id)

        return True


class OrganizerStore:
    """Storage for channel organizers."""

    def __init__(self) -> None:
        self._organizers: dict[tuple[int, int], ChannelOrganizers] = {}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        """Load organizers from file."""
        if not ORGANIZER_DATA_FILE.exists():
            self._organizers = {}
            return

        try:
            with open(ORGANIZER_DATA_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)

            loaded = {}
            for key, data in raw.items():
                # Format: "guild_id:channel_id"
                parts = key.split(":")
                if len(parts) == 2:
                    gid, cid = int(parts[0]), int(parts[1])
                    loaded[(gid, cid)] = ChannelOrganizers.from_dict(data)

            self._organizers = loaded
            logger.info("Загружено %d конфигураций организаторов", len(self._organizers))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.error("Ошибка загрузки organizer JSON: %s", exc)
            self._organizers = {}

    def save(self) -> None:
        """Save organizers to file."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        raw = {f"{gid}:{cid}": org.to_dict() for (gid, cid), org in self._organizers.items()}
        with open(ORGANIZER_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)

    def get(self, guild_id: int, channel_id: int) -> ChannelOrganizers | None:
        """Get organizers for a channel."""
        return self._organizers.get((guild_id, channel_id))

    def set(self, guild_id: int, channel_id: int, organizers: ChannelOrganizers) -> None:
        """Set organizers for a channel."""
        self._organizers[(guild_id, channel_id)] = organizers
        self.save()

    def delete(self, guild_id: int, channel_id: int) -> None:
        """Delete organizers for a channel."""
        self._organizers.pop((guild_id, channel_id), None)
        self.save()

    def all(self) -> list[tuple[int, int, ChannelOrganizers]]:
        """Get all channel organizers."""
        return [(gid, cid, org) for (gid, cid), org in self._organizers.items()]


# Глобальный экземпляр хранилища
organizer_store = OrganizerStore()
