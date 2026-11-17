"""Графы игроков на сайте: полный (GRAPHS_ENABLED) и за сутки (GRAPHS_DAILY_ENABLED), раз в 5 минут.

Данные читаются из MongoDB асинхронно; сборка графа и загрузка по SFTP тяжёлые и синхронные,
поэтому выполняются в отдельном потоке (asyncio.to_thread) и не блокируют бота.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from discord.ext import commands, tasks

from lobby_bot.features.seasons.service import seasons_repo
from lobby_bot.settings import settings
from lobby_bot.shared.web_upload import upload_to_site

from .daily import build_daily_graph_html
from .full import build_full_graph_html
from .repo import GraphsRepo

logger = logging.getLogger(__name__)

FULL_GRAPH_FILE = "players_graph.html"
DAILY_GRAPH_FILE = "players_graph_daily.html"


class GraphsCog(commands.Cog):
    def __init__(
        self, bot: commands.Bot, repo: GraphsRepo, upload: Callable[[str, bytes], None] = upload_to_site
    ) -> None:
        self.bot = bot
        self.repo = repo
        self.upload = upload

    async def cog_load(self) -> None:
        if settings.graphs_enabled:
            self.full_graph_task.start()
        if settings.graphs_daily_enabled:
            self.daily_graph_task.start()

    async def cog_unload(self) -> None:
        self.full_graph_task.cancel()
        self.daily_graph_task.cancel()

    @tasks.loop(seconds=300)
    async def full_graph_task(self) -> None:
        try:
            await self.publish_full_graph()
        except Exception:
            logger.exception("Ошибка обновления полного графа игроков")

    @tasks.loop(seconds=300)
    async def daily_graph_task(self) -> None:
        try:
            await self.publish_daily_graph()
        except Exception:
            logger.exception("Ошибка обновления графа игроков за сутки")

    async def publish_full_graph(self) -> None:
        data = await self.repo.load_full_graph_data()
        await asyncio.to_thread(
            self._render_and_upload, FULL_GRAPH_FILE, lambda: build_full_graph_html(data, datetime.now(UTC))
        )

    async def publish_daily_graph(self) -> None:
        end = datetime.now(UTC)
        lobbies, players = await self.repo.load_daily_graph_data(end - timedelta(hours=24), end)
        await asyncio.to_thread(
            self._render_and_upload, DAILY_GRAPH_FILE, lambda: build_daily_graph_html(lobbies, players)
        )

    def _render_and_upload(self, path: str, render: Callable[[], str]) -> None:
        self.upload(path, render().encode("utf-8"))


async def setup(bot: commands.Bot) -> None:
    db = bot.db
    await bot.add_cog(
        GraphsCog(bot, GraphsRepo(db.lobbies, db.players, db.history_player_stats, db.player_ratings, seasons_repo(db)))
    )
