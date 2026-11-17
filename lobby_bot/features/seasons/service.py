"""Текущий сезон: единственный источник правды для всего бота.

Сезон читается из базы при каждом вызове, поэтому /reset_season применяется сразу, без перезапуска.
Фича, которой нужен сезон, получает SeasonsRepo в `setup()` (`seasons_repo(bot.db)`) и вызывает
`await current_season(seasons)`. Не сохранять результат в константы модуля и не вычислять в значении
аргумента по умолчанию: писать `season=None` и внутри `if season is None: season = await current_season(...)`.
"""

import logging
from datetime import UTC, datetime

from .repo import SeasonsRepo

logger = logging.getLogger(__name__)


def seasons_repo(db) -> SeasonsRepo:
    """SeasonsRepo из lobby_bot.db.Database: одна точка сборки для всех фич."""
    return SeasonsRepo(db.seasons, db.elo_processing)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)  # в Mongo даты наивные в UTC


async def current_season(repo: SeasonsRepo) -> int:
    return await repo.active_season(_now())


async def season_or_first(repo: SeasonsRepo) -> int:
    """Сезон для команд и задач, которые должны работать и при недоступной базе сезонов: тогда сезон 1
    (так было в старом коде; ошибка пишется в лог)."""
    try:
        return await current_season(repo)
    except Exception:
        logger.exception("Не удалось получить текущий сезон, используется 1")
        return 1


async def start_new_season(repo: SeasonsRepo, season: int) -> int:
    """Начать сезон season. Возвращает номер завершённого сезона."""
    previous = await current_season(repo)
    await repo.start_season(season, _now())
    logger.info("Сезон %s завершён, начат сезон %s", previous, season)
    return previous
