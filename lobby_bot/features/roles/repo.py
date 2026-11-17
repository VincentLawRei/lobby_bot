"""Роли по реакциям в MongoDB.

- коллекция `roles_and_emojis`: {role, emoji, [steam_id], [description]}. Роль со steam_id это хост:
  её же читает мониторинг лобби (lobby_monitoring.py), поэтому формат документов не меняется;
- коллекция `bot_state`, ключ `last_roles_msg_id`: ID сообщения с выбором ролей в #roles.
"""

from pymongo.asynchronous.collection import AsyncCollection

# Эти роли показываются первыми, в этом порядке; остальные по алфавиту
PRIORITY_ROLES = ["Tournament", "TournamentWomen", "AllHosts", "TwitchNotificationBot", "NewPlayers"]
ROLES_MESSAGE_STATE_KEY = "last_roles_msg_id"


class RolesRepo:
    def __init__(self, roles: AsyncCollection, state: AsyncCollection) -> None:
        self._roles = roles
        self._state = state

    async def get_roles(self) -> dict[str, str]:
        """Роль -> эмодзи, в порядке показа."""
        docs = sorted(await self._roles.find({}).to_list(), key=lambda doc: _sort_key(doc["role"]))
        return {doc["role"]: doc["emoji"] for doc in docs}

    async def add_role(self, role: str, emoji: str, steam_id: str | None = None) -> None:
        fields = {"emoji": emoji}
        if steam_id is not None:
            fields["steam_id"] = steam_id
        await self._roles.update_one({"role": role}, {"$set": fields}, upsert=True)

    async def remove_role(self, role: str) -> None:
        await self._roles.delete_one({"role": role})

    async def set_description(self, role: str, description: str) -> bool:
        """False, если такой роли нет."""
        result = await self._roles.update_one({"role": role}, {"$set": {"description": description}})
        return result.matched_count > 0

    async def get_roles_message_id(self) -> int | None:
        doc = await self._state.find_one({"_id": ROLES_MESSAGE_STATE_KEY})
        return doc["value"] if doc else None

    async def set_roles_message_id(self, message_id: int) -> None:
        await self._state.update_one({"_id": ROLES_MESSAGE_STATE_KEY}, {"$set": {"value": message_id}}, upsert=True)


def _sort_key(role: str) -> tuple[int, str]:
    if role in PRIORITY_ROLES:
        return PRIORITY_ROLES.index(role), ""
    return len(PRIORITY_ROLES), role
