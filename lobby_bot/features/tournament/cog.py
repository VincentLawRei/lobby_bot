"""Список участников турнира (TOURNAMENT_ENABLED): все с ролью Tournament, одно сообщение
в TOURNAMENT_TRACK_CHANNEL_ID, обновляется раз в минуту."""

import logging

import discord
from discord.ext import commands, tasks

from lobby_bot.settings import settings

logger = logging.getLogger(__name__)

TOURNAMENT_ROLE = "Tournament"


class TournamentCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        if settings.tournament_enabled:
            self.participants_task.start()

    async def cog_unload(self) -> None:
        self.participants_task.cancel()

    @tasks.loop(seconds=60)
    async def participants_task(self) -> None:
        try:
            await self.update()
        except Exception:
            logger.exception("Ошибка обновления списка участников турнира")

    @participants_task.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def update(self) -> None:
        channel = self.bot.get_channel(settings.tournament_track_channel_id)
        if channel is None:
            return
        role = discord.utils.get(channel.guild.roles, name=TOURNAMENT_ROLE)
        if role is None:
            return
        embed = participants_embed([m for m in role.members if not m.bot])
        async for message in channel.history(limit=10):
            if message.author == self.bot.user and message.embeds:
                await message.edit(embed=embed)
                return
        await channel.send(embed=embed)


def participants_embed(members: list) -> discord.Embed:
    """Участники по алфавиту: ник на сервере, а если его нет, имя пользователя."""
    names = sorted((m.nick or m.name for m in members), key=str.lower)
    listing = "\n".join(f"- {name}" for name in names) or "*Нет участников*"
    return discord.Embed(
        title="Участники турнира",
        description=f"**Кол-во участников:** {len(names)}\n\n**Список:**\n{listing}",
        color=discord.Color.gold(),
    )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TournamentCog(bot))
