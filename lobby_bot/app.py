"""Сборка бота: класс LobbyBot и его единственный экземпляр `bot`.

Порядок старта процесса: `setup_hook` (один раз, после логина, до подключения к шлюзу) открывает
асинхронный клиент MongoDB (`bot.db`), загружает расширения (Cogs) из EXTENSIONS и синхронизирует
slash-команды. Клиент создаётся здесь, а не при импорте: ему нужен запущенный event loop. Закрывается
в `close`. Кеша серверов и каналов здесь ещё нет: то, что требует кеша (панель серверов, фоновые задачи),
фичи запускают сами в `cog_load` после `wait_until_ready`. on_ready может прийти повторно при
переподключении, поэтому разовые действия в нём не делаются.
"""

import logging

import discord
from discord.ext import commands

from lobby_bot.db import Database
from lobby_bot.settings import settings
from lobby_bot.shared.permissions import NotModerator

logger = logging.getLogger(__name__)

# Фичи в виде Cogs (lobby_bot/features/<имя>/cog.py). Выключить фичу: убрать строку из списка.
EXTENSIONS = [
    "lobby_bot.features.twitch.cog",
    "lobby_bot.features.roles.cog",
    "lobby_bot.features.players.cog",
    "lobby_bot.features.dm_log.cog",
    "lobby_bot.features.tournament.cog",
    "lobby_bot.features.lobbies.cog",
    "lobby_bot.features.graphs.cog",
    "lobby_bot.features.match_stats.cog",
    "lobby_bot.features.seasons.cog",
    "lobby_bot.features.elo.cog",
    "lobby_bot.features.leaderboard.cog",
    "lobby_bot.features.match_results.cog",
    "lobby_bot.features.console.cog",
    "lobby_bot.features.reports.cog",
    "lobby_bot.features.servers.cog",
]


class LobbyBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.db: Database | None = None

    async def setup_hook(self) -> None:
        # До загрузки фич: их setup() берёт коллекции из bot.db
        self.db = Database.connect(settings.mongo_url.get_secret_value())

        # Persistent-кнопки и меню (после рестарта старые сообщения продолжают работать) регистрирует
        # setup() самой фичи: панель серверов, лидерборд, итоги матчей, шаринг точек консоли.
        for extension in EXTENSIONS:
            await self.load_extension(extension)

        synced = await self.tree.sync()
        for cmd in synced:
            logger.info("%s: %s", cmd.name, cmd.id)

    async def close(self) -> None:
        await super().close()
        if self.db is not None:
            await self.db.close()

    async def on_ready(self) -> None:
        # on_ready приходит повторно при каждом переподключении к Discord, поэтому здесь только то,
        # что безопасно повторять. Фичи-Cogs слушают on_ready сами (@commands.Cog.listener).
        logger.info(f"✅ Logged in as {self.user}")

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        """Ошибки !-команд: понятный ответ пользователю, остальное в лог."""
        if isinstance(error, NotModerator):
            await ctx.send("❌ Недостаточно прав.")
        elif isinstance(error, commands.CommandNotFound):
            return  # опечатки и чужие команды с "!" не засоряют лог
        else:
            logger.error("Ошибка в команде %s", ctx.command, exc_info=error)


bot = LobbyBot()
