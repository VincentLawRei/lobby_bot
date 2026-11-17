"""Импорт файлов матчей из STATS_CSV_FOLDER (их кладёт deploy/move_csvs.sh, см. docs/SERVER.md).

Порядок за один проход:
1. `<id>.json` урона -> match_damage;
2. `<UUID>.csv` -> history_player_stats, валидный рейтинговый матч -> очередь ELO;
3. `<id>_pos.json` -> карта матча на сайте (после CSV, чтобы название карты уже было в базе).
Обработанный файл переносится в STATS_CSV_PROCESSED_FOLDER; файл с ошибкой остаётся и пробуется снова.

MongoDB асинхронный; чтение и разбор файлов и выгрузка карты по SFTP блокирующие и идут через asyncio.to_thread.
Перенос файлов в обработанные (локальный rename) остаётся синхронным.
"""

import asyncio
import json
import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from .parser import damage_doc, is_valid_match, normalize_map, parse_csv_file, parse_finish_time, player_stat_docs
from .repo import MatchStatsRepo
from .site import upload_match_map

logger = logging.getLogger(__name__)

POSITIONS_SUFFIX = "_pos.json"


class MatchImporter:
    def __init__(
        self,
        repo: MatchStatsRepo,
        folder: str,
        processed_folder: str,
        season: Callable[[], Awaitable[int]],
        publish_map: Callable[[str, str | None, str, str | None], str | None] = upload_match_map,
    ) -> None:
        self.repo = repo
        self.folder = folder
        self.processed_folder = processed_folder
        self.publish_map = publish_map
        self.season = season

    async def run(self) -> list[str]:
        """Один проход импорта. Возвращает UUID валидных нерейтинговых матчей (о них пишет бот)."""
        if not os.path.exists(self.folder):
            return []
        now = datetime.now(UTC).replace(tzinfo=None)  # в Mongo даты наивные в UTC
        for name in self._files(lambda f: f.endswith('.json') and not f.endswith(POSITIONS_SUFFIX)):
            await self._import_damage(os.path.join(self.folder, name), now)

        casual_matches = []
        imported = await self.repo.imported_match_uuids()
        for name in self._files(lambda f: f.endswith('.csv')):
            path = os.path.join(self.folder, name)
            try:
                casual = await self._import_csv(path, imported, now)
            except Exception:
                logger.exception("Не удалось импортировать матч %s, повтор на следующем проходе", name)
                continue
            if casual:
                casual_matches.append(casual)

        try:
            await self._publish_maps(now)
        except Exception:
            logger.exception("Ошибка выгрузки карт матчей")
        return casual_matches

    def _files(self, condition: Callable[[str], bool]) -> list[str]:
        return [f for f in os.listdir(self.folder) if condition(f)]

    # ---------- JSON урона ----------
    async def _import_damage(self, path: str, now: datetime) -> None:
        try:
            data = await asyncio.to_thread(_read_json, path)
            match_id = data.get('match_id')
            if not match_id:
                return  # не файл урона: оставляем как есть
            if not await self.repo.damage_imported(match_id):
                await self.repo.save_damage(damage_doc(data, now))
        except Exception:
            logger.exception("Не удалось импортировать урон из %s", path)
            return
        self._move_to_processed(path)

    # ---------- CSV ----------
    async def _import_csv(self, path: str, imported: set[str], now: datetime) -> str | None:
        """Импортировать матч; вернуть его UUID, если это валидный нерейтинговый матч."""
        match_data, players = await asyncio.to_thread(parse_csv_file, path)
        if not match_data:
            return None  # файл ещё не дописан
        match_uuid = match_data.get('UUID')
        if match_uuid in imported:
            self._move_to_processed(path)
            return None

        is_ranked = await self.repo.is_ranked(match_uuid)
        valid = is_valid_match(match_data, len(players))
        season = await self.season()
        await self.repo.save_player_stats(player_stat_docs(match_data, players, is_ranked, season, now))
        if is_ranked and valid:
            await self.repo.enqueue_for_elo(match_uuid, parse_finish_time(match_data))
        imported.add(match_uuid)
        self._move_to_processed(path)
        return match_uuid if valid and not is_ranked else None

    # ---------- Карты матчей ----------
    async def _publish_maps(self, now: datetime) -> None:
        """Выложить карты матчей, у которых уже есть урон (иначе на карте не будет ников)."""
        for name in self._files(lambda f: f.endswith(POSITIONS_SUFFIX)):
            match_id = name[: -len(POSITIONS_SUFFIX)]
            positions_path = os.path.join(self.folder, name)
            damage_path = self._damage_file(match_id)
            if damage_path is None and not await self.repo.damage_imported(match_id):
                continue
            map_name = normalize_map(await self.repo.map_of_match(match_id))
            url = await asyncio.to_thread(self.publish_map, match_id, damage_path, positions_path, map_name)
            if url:
                await self.repo.set_map_url(match_id, url, now)
                logger.info("[map] %s -> %s", match_id, url)
                self._move_to_processed(positions_path)

    def _damage_file(self, match_id: str) -> str | None:
        """JSON урона матча: ещё во входящих или уже в обработанных."""
        for folder in (self.folder, self.processed_folder):
            path = os.path.join(folder, f'{match_id}.json')
            if os.path.exists(path):
                return path
        return None

    def _move_to_processed(self, path: str) -> None:
        os.makedirs(self.processed_folder, exist_ok=True)
        try:
            shutil.move(path, os.path.join(self.processed_folder, os.path.basename(path)))
        except OSError:
            logger.exception("Не удалось перенести %s в обработанные", path)


def _read_json(path: str):
    with open(path, encoding='utf-8') as f:
        return json.load(f)
