"""Чтение данных для графов игроков из MongoDB (только чтение)."""

from dataclasses import dataclass
from datetime import datetime

from pymongo.asynchronous.collection import AsyncCollection

from lobby_bot.features.seasons.repo import SeasonsRepo
from lobby_bot.features.seasons.service import current_season

# Статистика игроков из истории матчей: по одной строке на steam_id
PLAYER_STATS_PIPELINE = [
    {
        '$group': {
            '_id': '$steam_id',
            'name': {'$last': '$name'},
            'total_games': {'$sum': 1},
            'total_wins': {'$sum': {'$cond': ['$won', 1, 0]}},
            'traitors_games': {'$sum': {'$cond': [{'$eq': ['$team', 'Traitors']}, 1, 0]}},
            'traitors_wins': {'$sum': {'$cond': [{'$and': [{'$eq': ['$team', 'Traitors']}, '$won']}, 1, 0]}},
            'crew_games': {'$sum': {'$cond': [{'$eq': ['$team', 'Crew']}, 1, 0]}},
            'crew_wins': {'$sum': {'$cond': [{'$and': [{'$eq': ['$team', 'Crew']}, '$won']}, 1, 0]}},
            'roles': {'$push': '$role'},
        }
    }
]


@dataclass
class FullGraphData:
    lobbies: list[dict]  # снапшоты лобби (коллекция lobbies)
    players: list[dict]  # игроки, новые записи первыми
    stats_rows: list[dict]  # результат PLAYER_STATS_PIPELINE
    elo_rows: list[dict]  # рейтинги текущего сезона, по убыванию ELO


class GraphsRepo:
    def __init__(
        self,
        lobbies: AsyncCollection,
        players: AsyncCollection,
        stats: AsyncCollection,
        ratings: AsyncCollection,
        seasons: SeasonsRepo,
    ) -> None:
        self._lobbies = lobbies
        self._players = players
        self._stats = stats
        self._ratings = ratings
        self._seasons = seasons  # рейтинги в граф берутся за текущий сезон

    async def load_full_graph_data(self) -> FullGraphData:
        return FullGraphData(
            lobbies=await self._lobbies.find().to_list(),
            players=await self._players.find().sort([("date", -1)]).to_list(),
            stats_rows=await (await self._stats.aggregate(PLAYER_STATS_PIPELINE)).to_list(),
            elo_rows=await self._load_elo_rows(),
        )

    async def load_daily_graph_data(self, start: datetime, end: datetime) -> tuple[list[dict], list[dict]]:
        """(лобби за [start, end), игроки с изменённым ником первыми, затем новые)."""
        lobbies = await self._lobbies.find({"timestamp": {"$gte": start, "$lt": end}}).to_list()
        players = await self._players.find().sort([("changed_nickname", -1), ("date", -1)]).to_list()
        return lobbies, players

    async def _load_elo_rows(self) -> list[dict]:
        season = await current_season(self._seasons)
        return (
            await self._ratings.find(
                {'season': season, 'ranked_games': {'$gt': 0}},
                {
                    'steam_id': 1,
                    'current_name': 1,
                    'changed_nickname': 1,
                    'elo': 1,
                    'ranked_games': 1,
                    'ranked_wins': 1,
                },
            )
            .sort('elo', -1)
            .to_list()
        )
