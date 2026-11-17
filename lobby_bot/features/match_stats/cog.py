"""Статистика матчей: раз в минуту импорт файлов матчей (см. importer.py). Валидные нерейтинговые
матчи уходят событием `casual_matches_imported` (итоги в канал отправляет фича match_results)."""

import logging

from discord.ext import commands, tasks

from lobby_bot.features.seasons.service import current_season, seasons_repo
from lobby_bot.settings import settings

from .importer import MatchImporter
from .repo import MatchStatsRepo

logger = logging.getLogger(__name__)


class MatchStatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot, importer: MatchImporter) -> None:
        self.bot = bot
        self.importer = importer
        self._indexes_ready = False

    async def cog_load(self) -> None:
        self.import_task.start()

    async def cog_unload(self) -> None:
        self.import_task.cancel()

    @tasks.loop(seconds=60)
    async def import_task(self) -> None:
        try:
            casual_matches = await self._import()
        except Exception:
            logger.exception("Ошибка импорта статистики матчей")
            return
        if casual_matches:  # итоги отправляет фича match_results
            self.bot.dispatch("casual_matches_imported", casual_matches)

    @import_task.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _import(self) -> list[str]:
        if not self._indexes_ready:  # один раз за запуск, но не в cog_load: недоступная база не должна мешать старту
            await self.importer.repo.ensure_indexes()
            self._indexes_ready = True
        return await self.importer.run()


async def setup(bot: commands.Bot) -> None:
    db = bot.db
    repo = MatchStatsRepo(
        db.history_player_stats, db.elo_processing, db.game_matches, db.player_ratings, db.elo_history, db.match_damage
    )
    seasons = seasons_repo(db)
    importer = MatchImporter(
        repo, settings.stats_csv_folder, settings.stats_csv_processed_folder, season=lambda: current_season(seasons)
    )
    await bot.add_cog(MatchStatsCog(bot, importer))
