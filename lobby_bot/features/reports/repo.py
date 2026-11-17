"""Репорты на игроков в MongoDB: `player_reports_col` (репорты), `player_ratings` (поиск игрока, только чтение)."""

import re

from pymongo.asynchronous.collection import AsyncCollection


class ReportsRepo:
    def __init__(self, reports: AsyncCollection, ratings: AsyncCollection) -> None:
        self._reports = reports
        self._ratings = ratings

    async def save(self, doc: dict) -> None:
        await self._reports.insert_one(doc)

    async def player(self, steam_id: str, season: int) -> dict | None:
        return await self._ratings.find_one({"steam_id": steam_id, "season": season})

    async def search(self, season: int, current: str) -> list[dict]:
        """До 25 игроков сезона, чей ник (из игры или отображаемый) содержит current."""
        query = {"season": season}
        if current:
            pattern = re.escape(current)  # ввод пользователя ищется как текст, а не как регулярное выражение
            query["$or"] = [
                {"current_name": {"$regex": pattern, "$options": "i"}},
                {"changed_nickname": {"$regex": pattern, "$options": "i"}},
            ]
        return (
            await self._ratings.find(query, {"steam_id": 1, "current_name": 1, "changed_nickname": 1})
            .limit(25)
            .to_list()
        )
