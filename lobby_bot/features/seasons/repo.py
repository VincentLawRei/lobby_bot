"""Сезоны в MongoDB (коллекция `seasons`): {season, is_active, started_at, [ended_at]}.

Активный сезон всегда один. Очередь ELO (`elo_processing`) затрагивается только при смене сезона.
"""

from datetime import datetime

from pymongo.asynchronous.collection import AsyncCollection


class SeasonsRepo:
    def __init__(self, seasons: AsyncCollection, elo_queue: AsyncCollection) -> None:
        self._seasons = seasons
        self._elo_queue = elo_queue

    async def active_season(self, now: datetime) -> int:
        """Номер активного сезона. Если сезонов ещё нет, создаётся первый (как и раньше)."""
        doc = await self._seasons.find_one({'is_active': True})
        if doc:
            return doc['season']
        await self._seasons.insert_one({'season': 1, 'is_active': True, 'started_at': now})
        return 1

    async def start_season(self, season: int, now: datetime) -> None:
        """Закрыть активный сезон и начать новый. Необработанные матчи старого сезона в ELO не попадут."""
        await self._elo_queue.update_many(
            {'processed': False},
            {'$set': {'processed': True, 'skipped': True, 'reason': 'season_reset'}},
        )
        await self._seasons.update_many({'is_active': True}, {'$set': {'is_active': False, 'ended_at': now}})
        await self._seasons.insert_one({'season': season, 'is_active': True, 'started_at': now})
