"""ELO в MongoDB.

- `player_ratings`: рейтинг игрока в сезоне (ключ steam_id + season);
- `history_player_stats`: строки игроков матча, сюда дописываются elo_before/elo_after/elo_change;
- `elo_history`: история изменений ELO (матчи, твинки, decay);
- `matches`: агрегаты рейтинговых матчей;
- `elo_processing`: очередь матчей на расчёт (заполняет match_stats);
- `elo_overrides`: ручные бонусы веса на предателя {steam_id, bonus}, правятся вручную в MongoDB.
"""

import re
from datetime import datetime

from pymongo.asynchronous.collection import AsyncCollection


class EloRepo:
    def __init__(
        self,
        ratings: AsyncCollection,
        stats: AsyncCollection,
        elo_history: AsyncCollection,
        matches: AsyncCollection,
        queue: AsyncCollection,
        overrides: AsyncCollection,
    ) -> None:
        self._ratings = ratings
        self._stats = stats
        self._elo_history = elo_history
        self._matches = matches
        self._queue = queue
        self._overrides = overrides

    # ---------- Рейтинги ----------
    async def rating(self, steam_id: str, season: int) -> dict | None:
        return await self._ratings.find_one({'steam_id': steam_id, 'season': season})

    async def insert_rating(self, doc: dict) -> None:
        await self._ratings.insert_one(doc)

    async def update_rating(self, steam_id: str, season: int, fields: dict) -> None:
        await self._ratings.update_one({'steam_id': steam_id, 'season': season}, {'$set': fields})

    async def decay_candidates(
        self, season: int, min_elo: int, inactive_before: datetime, decayed_before: datetime
    ) -> list[dict]:
        """Игроки выше порога, без матчей с inactive_before и без среза с decayed_before."""
        return await self._ratings.find(
            {
                'season': season,
                'elo': {'$gt': min_elo},
                'last_match_date': {'$lt': inactive_before},
                '$or': [
                    {'last_decay_date': {'$exists': False}},
                    {'last_decay_date': {'$lt': decayed_before}},
                ],
            }
        ).to_list()

    # ---------- Матчи ----------
    async def ranked_match_players(self, match_uuid: str) -> list[dict]:
        return await self._stats.find({'match_uuid': match_uuid, 'is_ranked': True, 'is_valid_match': True}).to_list()

    async def set_player_match_elo(self, match_uuid: str, steam_id: str, fields: dict) -> None:
        await self._stats.update_one({'match_uuid': match_uuid, 'steam_id': steam_id}, {'$set': fields})

    async def save_match(self, match_uuid: str, fields: dict) -> None:
        await self._matches.update_one({'match_uuid': match_uuid}, {'$set': fields}, upsert=True)

    async def add_elo_history(self, doc: dict) -> None:
        await self._elo_history.insert_one(doc)

    async def add_match_elo_history(self, doc: dict) -> None:
        """Запись elo_history за матч: одна на (match_uuid, steam_id, season).
        Повторная запись того же матча ничего не меняет (повтор расчёта после сбоя)."""
        key = {'match_uuid': doc['match_uuid'], 'steam_id': doc['steam_id'], 'season': doc['season']}
        rest = {k: v for k, v in doc.items() if k not in key}
        await self._elo_history.update_one(key, {'$setOnInsert': rest}, upsert=True)

    async def ensure_indexes(self) -> None:
        """Индексы под запросы ELO, которых нет в MatchStatsRepo.ensure_indexes (повторный вызов ничего не меняет).
        Не уникальные: в старых данных могут быть дубли, уникальный индекс на них не создастся."""
        await self._elo_history.create_index([('match_uuid', 1), ('steam_id', 1), ('season', 1)])
        await self._elo_history.create_index([('season', 1), ('match_datetime', -1)])
        await self._matches.create_index('match_uuid')

    # ---------- Очередь ----------
    async def pending_matches(self) -> list[dict]:
        return await self._queue.find({'processed': False}).sort('match_datetime', 1).to_list()

    async def queued_plan(self, match_uuid: str) -> dict | None:
        """Сохранённый расчёт матча, который ещё не дописан в базу (см. service.process_queued_match)."""
        doc = await self._queue.find_one({'match_uuid': match_uuid}, {'elo_plan': 1})
        return doc.get('elo_plan') if doc else None

    async def save_plan(self, match_uuid: str, plan: dict) -> None:
        await self._queue.update_one({'match_uuid': match_uuid}, {'$set': {'elo_plan': plan}})

    async def mark_processed(self, match_uuid: str, season: int, now: datetime) -> None:
        await self._queue.update_one(
            {'match_uuid': match_uuid},
            {'$set': {'processed': True, 'processed_at': now, 'season': season}, '$unset': {'elo_plan': ''}},
        )

    # ---------- Модераторские команды ----------
    async def search_players(
        self, season: int, current: str, with_nickname: bool = False, only_penalized: bool = False
    ) -> list[dict]:
        """До 25 игроков сезона, у которых steam_id или ник содержит current (без учёта регистра)."""
        query = {'season': season}
        if only_penalized:
            query['is_penalized'] = True
        if current:
            fields = ['steam_id', 'current_name'] + (['changed_nickname'] if with_nickname else [])
            query['$or'] = [{f: {'$regex': re.escape(current), '$options': 'i'}} for f in fields]
        projection = {'steam_id': 1, 'current_name': 1}
        if with_nickname:
            projection.update({'changed_nickname': 1, 'is_penalized': 1, 'penalty_reason': 1})
        return await self._ratings.find(query, projection).limit(25).to_list()

    async def match_entries(self, uuid_prefix: str, season: int) -> list[dict]:
        """Изменения ELO по матчам, чей UUID начинается с uuid_prefix (без ручных правок)."""
        return await self._elo_history.find(
            {
                'match_uuid': {'$regex': f'^{re.escape(uuid_prefix)}', '$options': 'i'},
                'season': season,
                'manual': {'$exists': False},
            }
        ).to_list()

    async def revert_count(self, match_uuid: str, steam_id: str, season: int) -> int:
        return await self._elo_history.count_documents(
            {'reverted_match_uuid': match_uuid, 'steam_id': steam_id, 'season': season}
        )

    async def recent_match_entries(self, season: int, uuid_prefix: str, limit: int = 200) -> list[dict]:
        query = {'season': season, 'manual': {'$exists': False}}
        if uuid_prefix:
            query['match_uuid'] = {'$regex': f'^{re.escape(uuid_prefix)}', '$options': 'i'}
        return await (
            self._elo_history.find(query, {'match_uuid': 1, 'match_datetime': 1})
            .sort('match_datetime', -1)
            .limit(limit)
            .to_list()
        )

    async def expired_penalties(self, season: int, now: datetime) -> list[dict]:
        return await self._ratings.find(
            {'season': season, 'is_penalized': True, 'penalty_until': {'$lte': now, '$ne': None}}
        ).to_list()

    # ---------- Ручные бонусы ----------
    async def overrides(self) -> dict[str, int]:
        return {doc['steam_id']: doc['bonus'] async for doc in self._overrides.find()}
