"""Консоль серверов в MongoDB.

- `console_access`: кому хост/модератор выдал доступ к консоли сервера {server_num, user_ids, granted_by};
- `player_locations`: сохранённые точки телепорта пользователя {user_id, locations: [{name, coords}],
  last_coords}; глобальные точки — документ с user_id="__global__".
"""

from datetime import datetime

from pymongo.asynchronous.collection import AsyncCollection

GLOBAL_KEY = "__global__"


class ConsoleRepo:
    def __init__(self, access: AsyncCollection, locations: AsyncCollection) -> None:
        self._access = access
        self._locations = locations

    # ---------- Доступ ----------
    async def access_user_ids(self, server_num: int) -> list[str]:
        doc = await self._access.find_one({"server_num": server_num})
        return doc.get("user_ids", []) if doc else []

    async def grant_access(self, server_num: int, user_ids: list[str], granted_by: str, now: datetime) -> None:
        await self._access.update_one(
            {"server_num": server_num},
            {"$set": {"user_ids": user_ids, "granted_by": granted_by, "updated_at": now}},
            upsert=True,
        )

    async def revoke_access(self, server_num: int) -> None:
        await self._access.delete_one({"server_num": server_num})

    # ---------- Точки ----------
    async def locations(self, owner: str) -> dict[str, str]:
        """name -> coords. В базе массивом, чтобы имена могли быть любыми."""
        doc = await self._locations.find_one({"user_id": owner})
        return {it["name"]: it["coords"] for it in (doc.get("locations", []) if doc else [])}

    async def put_location(self, owner: str, name: str, coords: str, now: datetime) -> None:
        """Добавить точку; точка с тем же именем заменяется (уходит в конец списка)."""
        await self.remove_location(owner, name)
        await self._locations.update_one(
            {"user_id": owner},
            {"$push": {"locations": {"name": name, "coords": coords}}, "$set": {"updated_at": now}},
            upsert=True,
        )

    async def remove_location(self, owner: str, name: str) -> None:
        await self._locations.update_one({"user_id": owner}, {"$pull": {"locations": {"name": name}}})

    async def set_last_coords(self, user_id: str, coords: str) -> None:
        await self._locations.update_one({"user_id": user_id}, {"$set": {"last_coords": coords}}, upsert=True)

    async def last_coords(self, user_id: str) -> str | None:
        doc = await self._locations.find_one({"user_id": user_id})
        return doc.get("last_coords") if doc else None
