"""Игроки из мониторинга лобби (коллекция `players`): ник, Social Deduction Game ID, Steam ID (other_id)."""

from pymongo.asynchronous.collection import AsyncCollection


class PlayersRepo:
    def __init__(self, collection: AsyncCollection) -> None:
        self._col = collection

    async def search_by_nickname(self, nickname_part: str, limit: int = 50) -> list[dict]:
        """Записи, где ник содержит nickname_part (регистр не важен), новые первыми.
        nickname_part работает как регулярное выражение (как и раньше)."""
        query = {"nickname": {"$regex": nickname_part, "$options": "i"}}
        return await self._col.find(query).sort("date", -1).limit(limit).to_list()

    async def find_steam_id(self, identifier: str) -> str | None:
        """Steam ID игрока по его Social Deduction Game ID или Steam ID; None, если игрока нет."""
        doc = await self._col.find_one({"game_uid": identifier}) or await self._col.find_one({"other_id": identifier})
        return doc.get("other_id") if doc else None

    async def set_for_all_records(self, steam_id: str, field: str, value: str) -> int:
        """Записать поле во все записи игрока. Возвращает число изменённых записей."""
        return (await self._col.update_many({"other_id": steam_id}, {"$set": {field: value}})).modified_count
