"""Лидерборд: чтение рейтингов и статистики из MongoDB.

- `player_ratings`: рейтинги (таблица, карточки, отображаемые ники `changed_nickname`);
- `history_player_stats`: строки игроков матчей (дуэты, стрики, винрейт сторон, состав матча);
- `elo_history`: изменения ELO игрока (история матчей, график);
- `discord_panel`: ID сообщений лидерборда и статистики в каналах (ключи `leaderboard_message_id`, `duos_message_id`);
- `profile_views`: счётчик просмотров карточек.

Текущий сезон: `seasons` (SeasonsRepo фичи seasons), читается через `seasons.service.current_season`.
"""

from datetime import datetime

from pymongo import ReturnDocument
from pymongo.asynchronous.collection import AsyncCollection

from lobby_bot.features.seasons.repo import SeasonsRepo

RANKED = {'is_ranked': True, 'is_valid_match': True}
# Матчи в истории игрока: без ручных правок, decay и начислений за твинка
MATCH_HISTORY = {'manual': {'$exists': False}, 'twink_penalty': {'$ne': True}}


class LeaderboardRepo:
    def __init__(
        self,
        ratings: AsyncCollection,
        stats: AsyncCollection,
        elo_history: AsyncCollection,
        panel: AsyncCollection,
        profile_views: AsyncCollection,
        seasons: SeasonsRepo,
    ) -> None:
        self.seasons = seasons
        self._ratings = ratings
        self._stats = stats
        self._elo_history = elo_history
        self._panel = panel
        self._views = profile_views

    # ---------- Рейтинги ----------
    async def ranked_players(self, season: int) -> list[dict]:
        """Игроки сезона с рейтинговыми играми, по убыванию ELO."""
        return await self._ratings.find({'season': season, 'ranked_games': {'$gt': 0}}).sort('elo', -1).to_list()

    async def nicknames(self, names: list[str]) -> dict[str, str]:
        """current_name -> changed_nickname для игроков с отображаемым ником (по всем сезонам)."""
        result = {}
        query = {'current_name': {'$in': names}, 'changed_nickname': {'$exists': True}}
        async for rating in self._ratings.find(query, {'current_name': 1, 'changed_nickname': 1}):
            if rating.get('changed_nickname'):
                result[rating['current_name']] = rating['changed_nickname']
        return result

    async def set_nickname(self, steam_id: str, nickname: str | None) -> None:
        """Отображаемый ник во всех сезонах; None сбрасывает."""
        update = {'$set': {'changed_nickname': nickname}} if nickname else {'$unset': {'changed_nickname': ''}}
        await self._ratings.update_many({'steam_id': steam_id}, update)

    async def most_traitors_games(self, season: int, limit: int) -> list[dict]:
        return (
            await self._ratings.find(
                {'season': season, 'traitors_games': {'$gt': 0}},
                {'current_name': 1, 'traitors_games': 1, 'ranked_games': 1},
            )
            .sort('traitors_games', -1)
            .limit(limit)
            .to_list()
        )

    async def players_with_games(self, season: int, min_games: int) -> list[dict]:
        return await self._ratings.find(
            {'season': season, 'ranked_games': {'$gte': min_games}},
            {'current_name': 1, 'traitors_games': 1, 'ranked_games': 1},
        ).to_list()

    async def past_seasons_of(self, steam_id: str, before_season: int) -> list[dict]:
        return await self._ratings.find(
            {'steam_id': steam_id, 'ranked_games': {'$gt': 0}, 'season': {'$lt': before_season}}
        ).to_list()

    async def rank(self, season: int, elo: int) -> int:
        """Место игрока с таким ELO в сезоне."""
        query = {'season': season, 'ranked_games': {'$gt': 0}, 'elo': {'$gt': elo}}
        return await self._ratings.count_documents(query) + 1

    async def ranked_seasons(self) -> list:
        return await self._ratings.distinct('season', {'ranked_games': {'$gt': 0}})

    # ---------- Статистика матчей ----------
    async def outcome_counts(self, season: int) -> dict[str, int]:
        """Исход матча -> число матчей сезона."""
        pipeline = [
            {"$match": {**RANKED, "season": season}},
            {"$group": {"_id": "$match_uuid", "outcome": {"$first": "$match_outcome"}}},
            {"$group": {"_id": "$outcome", "count": {"$sum": 1}}},
        ]
        return {r['_id']: r['count'] async for r in await self._stats.aggregate(pipeline)}

    async def team_rows(self, season: int, team: str) -> list[dict]:
        return await self._stats.find(
            {'season': season, 'team': team, **RANKED}, {'match_uuid': 1, 'steam_id': 1, 'name': 1, 'won': 1}
        ).to_list()

    async def team_rows_since(self, season: int, team: str, since: datetime) -> list[dict]:
        """Строки команды с даты since, новые первыми."""
        return (
            await self._stats.find(
                {'season': season, 'team': team, **RANKED, 'match_datetime': {'$gte': since}},
                {'steam_id': 1, 'name': 1, 'won': 1, 'match_datetime': 1},
            )
            .sort('match_datetime', -1)
            .to_list()
        )

    async def team_totals_since(self, season: int, team: str, since: datetime, min_games: int) -> list[dict]:
        """Игры и победы игроков за команду с даты since (не меньше min_games игр)."""
        return await (
            await self._stats.aggregate(
                [
                    {"$match": {"season": season, "team": team, **RANKED, "match_datetime": {"$gte": since}}},
                    {
                        "$group": {
                            "_id": "$steam_id",
                            "name": {"$last": "$name"},
                            "games": {"$sum": 1},
                            "wins": {"$sum": {"$cond": ["$won", 1, 0]}},
                        }
                    },
                    {"$match": {"games": {"$gte": min_games}}},
                ]
            )
        ).to_list()

    async def top_by_rows(self, match: dict, limit: int) -> list[dict]:
        """Игроки с наибольшим числом строк, подходящих под match: [{_id, count, name}]."""
        return await (
            await self._stats.aggregate(
                [
                    {'$match': match},
                    {'$group': {'_id': '$steam_id', 'count': {'$sum': 1}, 'name': {'$last': '$name'}}},
                    {'$sort': {'count': -1}},
                    {'$limit': limit},
                ]
            )
        ).to_list()

    async def match_others(self, match_uuid: str, steam_id: str) -> list[dict]:
        """Остальные игроки матча: [{name, team}]."""
        return await self._stats.find(
            {'match_uuid': match_uuid, 'steam_id': {'$ne': steam_id}}, {'name': 1, 'team': 1}
        ).to_list()

    # ---------- История ELO ----------
    async def elo_points(self, steam_id: str, season: int) -> list[dict]:
        return (
            await self._elo_history.find(
                {'steam_id': steam_id, 'season': season}, {'elo_after': 1, 'match_datetime': 1, 'won': 1}
            )
            .sort('match_datetime', 1)
            .to_list()
        )

    async def recent_matches(self, steam_id: str, season: int, skip: int, limit: int) -> list[dict]:
        return await self._elo_history.find(
            {'steam_id': steam_id, 'season': season, **MATCH_HISTORY},
            sort=[('match_datetime', -1)],
            skip=skip,
            limit=limit,
        ).to_list()

    async def matches_count(self, steam_id: str, season: int, with_twink: bool = False) -> int:
        """Число матчей в истории. with_twink: считать и начисления за твинка (так считали кнопки страниц)."""
        query = {'steam_id': steam_id, 'season': season, 'manual': {'$exists': False}}
        if not with_twink:
            query['twink_penalty'] = {'$ne': True}
        return await self._elo_history.count_documents(query)

    # ---------- Сообщения в каналах и просмотры ----------
    async def message_id(self, key: str) -> int | None:
        doc = await self._panel.find_one({'key': key})
        return doc['value'] if doc else None

    async def save_message_id(self, key: str, message_id: int | None) -> None:
        await self._panel.update_one({'key': key}, {'$set': {'value': message_id}}, upsert=True)

    async def add_profile_view(self, steam_id: str) -> int:
        doc = await self._views.find_one_and_update(
            {'steam_id': steam_id}, {'$inc': {'views': 1}}, upsert=True, return_document=ReturnDocument.AFTER
        )
        return doc.get('views', 1) if doc else 1
