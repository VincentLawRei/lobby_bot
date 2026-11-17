"""/reset_season: начать новый сезон ELO (только модераторы)."""

import discord
from discord import app_commands
from discord.ext import commands

from lobby_bot.shared.permissions import is_moderator

from .repo import SeasonsRepo
from .service import seasons_repo, start_new_season


class SeasonsCog(commands.Cog):
    def __init__(self, bot: commands.Bot, repo: SeasonsRepo) -> None:
        self.bot = bot
        self.repo = repo

    @app_commands.command(name='reset_season', description='[Мод] Начать новый сезон')
    @app_commands.describe(new_season='Номер нового сезона')
    async def reset_season(self, interaction: discord.Interaction, new_season: int) -> None:
        if not is_moderator(interaction.user):
            await interaction.response.send_message('❌ Недостаточно прав.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        previous = await start_new_season(self.repo, new_season)
        await interaction.followup.send(
            f'✅ Сезон **{previous}** завершён. Начат сезон **{new_season}**.', ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SeasonsCog(bot, seasons_repo(bot.db)))
