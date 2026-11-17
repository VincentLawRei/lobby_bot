"""Итоги матчей: чтение статистики, ELO и урона.

- `history_player_stats`: игроки матча (имена, длительность, дата, твинки);
- `player_ratings`: отображаемые ники `changed_nickname` и штрафы;
- `elo_history`: изменения ELO игроков матча и начисления основным аккаунтам за твинков;
- `match_damage`: урон из JSON (`players`, `raw_damage`).
Репорты на игроков из матча сохраняет фича reports.
"""

from pymongo.asynchronous.collection import AsyncCollection


class MatchResultsRepo:
    def __init__(
        self, stats: AsyncCollection, ratings: AsyncCollection, elo_history: AsyncCollection, damage: AsyncCollection
    ) -> None:
        self._stats = stats
        self._ratings = ratings
        self._elo_history = elo_history
        self._damage = damage

    async def match_rows(self, match_uuid: str) -> list[dict]:
        return await self._stats.find({'match_uuid': match_uuid}).to_list()

    async def twink_rows(self, match_uuid: str) -> list[dict]:
        """Игроки матча на твинках (их ELO ушло основному)."""
        return await self._stats.find({'match_uuid': match_uuid, 'twink_redirected_to': {'$exists': True}}).to_list()

    async def nicknames(self, steam_ids: list[str], season: int | None = None) -> dict[str, str]:
        """steam_id -> отображаемый ник. Без season — по всем сезонам (последний найденный)."""
        query = {'steam_id': {'$in': steam_ids}}
        if season is not None:
            query['season'] = season
        return {
            r['steam_id']: r['changed_nickname'] async for r in self._ratings.find(query) if r.get('changed_nickname')
        }

    async def penalized(self, steam_ids: list[str], season: int) -> list[dict]:
        return [
            r
            async for r in self._ratings.find({'steam_id': {'$in': steam_ids}, 'season': season})
            if r.get('is_penalized')
        ]

    async def elo_records(self, match_uuid: str, season: int) -> list[dict]:
        return await self._elo_history.find({'match_uuid': match_uuid, 'season': season}).to_list()

    async def twink_main_changes(self, match_uuid: str, season: int, main_ids: list[str]) -> dict[str, dict]:
        """Начисления основным аккаунтам за игру твинков: steam_id -> запись elo_history."""
        return {
            h['steam_id']: h
            async for h in self._elo_history.find(
                {'match_uuid': match_uuid, 'season': season, 'steam_id': {'$in': main_ids}, 'twink_penalty': True}
            )
        }

    async def damage(self, match_id: str) -> dict | None:
        return await self._damage.find_one({'match_id': match_id})
