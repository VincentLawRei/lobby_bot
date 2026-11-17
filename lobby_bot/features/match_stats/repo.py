"""Статистика матчей в MongoDB.

- `history_player_stats`: строка на игрока в матче (по ним считаются ELO, лидерборд, графы);
- `elo_processing`: очередь валидных рейтинговых матчей для расчёта ELO;
- `match_damage`: урон из JSON и ссылка на карту матча на сайте;
- `game_matches`: лобби, запущенные через бота; is_ranked_mods решает, рейтинговый ли матч.
"""

import contextlib
from datetime import datetime

from pymongo.asynchronous.collection import AsyncCollection
from pymongo.errors import DuplicateKeyError


class MatchStatsRepo:
    def __init__(
        self,
        history: AsyncCollection,
        elo_queue: AsyncCollection,
        game_matches: AsyncCollection,
        ratings: AsyncCollection,
        elo_history: AsyncCollection,
        damage: AsyncCollection,
    ) -> None:
        self._history = history
        self._elo_queue = elo_queue
        self._game_matches = game_matches
        self._ratings = ratings
        self._elo_history = elo_history
        self._damage = damage

    async def ensure_indexes(self) -> None:
        """Индексы для импорта, ELO и лидерборда (повторный вызов ничего не меняет)."""
        await self._history.create_index([("match_uuid", 1), ("steam_id", 1)], unique=True)
        await self._history.create_index([("match_datetime", 1)])
        await self._history.create_index([("is_ranked", 1), ("is_valid_match", 1)])
        await self._history.create_index("season")
        await self._history.create_index("steam_id")
        await self._elo_queue.create_index("match_uuid", unique=True)
        await self._elo_queue.create_index([("processed", 1), ("match_datetime", 1)])
        await self._ratings.create_index([("steam_id", 1), ("season", 1)], unique=True)
        await self._ratings.create_index([("season", 1), ("elo", -1)])
        await self._elo_history.create_index([("steam_id", 1), ("match_datetime", 1)])
        await self._elo_history.create_index("season")
        await self._damage.create_index("match_id", unique=True)

    # ---------- Матчи из CSV ----------
    async def imported_match_uuids(self) -> set[str]:
        return set(await self._history.distinct("match_uuid"))

    async def is_ranked(self, match_uuid: str) -> bool:
        """Рейтинговый, если лобби запускалось через бота с рейтинговыми модами."""
        doc = await self._game_matches.find_one({'guuid': match_uuid})
        return doc.get('is_ranked_mods', False) if doc else False

    async def save_player_stats(self, docs: list[dict]) -> None:
        for doc in docs:
            await self._history.insert_one(doc)

    async def enqueue_for_elo(self, match_uuid: str, match_datetime: datetime | None) -> None:
        with contextlib.suppress(DuplicateKeyError):  # матч уже в очереди
            await self._elo_queue.insert_one(
                {'match_uuid': match_uuid, 'match_datetime': match_datetime, 'processed': False}
            )

    async def map_of_match(self, match_id: str) -> str | None:
        """Карта матча из статистики (match_uuid из CSV совпадает с match_id из JSON)."""
        doc = await self._history.find_one({'match_uuid': match_id}, {'map': 1})
        return doc.get('map') if doc else None

    # ---------- Урон из JSON ----------
    async def damage_imported(self, match_id: str) -> bool:
        return await self._damage.find_one({'match_id': match_id}) is not None

    async def save_damage(self, doc: dict) -> None:
        await self._damage.insert_one(doc)

    async def set_map_url(self, match_id: str, url: str, now: datetime) -> None:
        await self._damage.update_one({'match_id': match_id}, {'$set': {'map_url': url, 'map_uploaded_at': now}})
