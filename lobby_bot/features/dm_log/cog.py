"""Личные сообщения боту: сохраняются в MongoDB (коллекция `dm_logs`), автору приходит подтверждение."""

from datetime import UTC, datetime

import discord
from discord.ext import commands
from pymongo.asynchronous.collection import AsyncCollection


class DmLogCog(commands.Cog):
    def __init__(self, bot: commands.Bot, collection: AsyncCollection) -> None:
        self.bot = bot
        self._col = collection

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.bot.user or not isinstance(message.channel, discord.DMChannel):
            return
        await self._col.insert_one(
            {
                "user_id": message.author.id,
                "username": str(message.author),
                "message": message.content,
                "timestamp": datetime.now(UTC),
            }
        )
        await message.channel.send("Спасибо, твоё сообщение сохранено!")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DmLogCog(bot, bot.db.dm_logs))
