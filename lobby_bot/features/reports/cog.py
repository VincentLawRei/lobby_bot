"""/report: репорт на игрока по нику (выбор причин), пост в REPORTS_LOG_CHANNEL_ID."""

import logging
from datetime import UTC

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Select, View

from lobby_bot.features.seasons.repo import SeasonsRepo
from lobby_bot.features.seasons.service import season_or_first, seasons_repo
from lobby_bot.settings import settings

from . import service
from .repo import ReportsRepo

logger = logging.getLogger(__name__)


def build_report_embed(
    target_name: str, reasons: list[str], anonymous: bool, reporter: discord.abc.User
) -> discord.Embed:
    """Пост о репорте в лог-канал (общий для /report и кнопки репорта из матча)."""
    embed = discord.Embed(title="🚩 Новый репорт", color=discord.Color.red())
    embed.add_field(name="На игрока", value=f"**{target_name}**", inline=True)
    embed.add_field(name="От", value="Аноним" if anonymous else reporter.mention, inline=True)
    embed.add_field(name="Причины", value="\n".join(f"• {r}" for r in reasons), inline=False)
    embed.timestamp = service.now_utc().replace(tzinfo=UTC)  # naive discord.py считает локальным временем
    return embed


class ReportReasonSelect(Select):
    def __init__(self, repo: ReportsRepo, target_steam_id: str, target_name: str, anonymous: bool, log_channel_id: int):
        self.repo = repo
        self.target_steam_id = target_steam_id
        self.target_name = target_name
        self.anonymous = anonymous
        self.log_channel_id = log_channel_id
        options = [discord.SelectOption(label=r) for r in service.REPORT_REASONS]
        super().__init__(
            placeholder="Выберите причину(ы) репорта", min_values=1, max_values=len(options), options=options
        )

    async def callback(self, interaction: discord.Interaction):
        reasons = self.values
        await service.save_report(
            self.repo,
            str(interaction.user.id),
            interaction.user.display_name,
            self.anonymous,
            self.target_steam_id,
            self.target_name,
            reasons,
        )
        log_channel = interaction.client.get_channel(self.log_channel_id)
        if log_channel:
            await log_channel.send(
                embed=build_report_embed(self.target_name, reasons, self.anonymous, interaction.user)
            )
        await interaction.response.edit_message(content="✅ Репорт отправлен. Спасибо!", view=None)


class ReportReasonView(View):
    def __init__(self, repo: ReportsRepo, target_steam_id: str, target_name: str, anonymous: bool, log_channel_id: int):
        super().__init__(timeout=120)
        self.add_item(ReportReasonSelect(repo, target_steam_id, target_name, anonymous, log_channel_id))


class ReportsCog(commands.Cog):
    def __init__(self, bot: commands.Bot, repo: ReportsRepo, seasons: SeasonsRepo, log_channel_id: int) -> None:
        self.bot = bot
        self.repo = repo
        self.seasons = seasons
        self.log_channel_id = log_channel_id

    @app_commands.command(name="report", description="Подать репорт на игрока")
    @app_commands.describe(
        player="Игрок, на которого подаётся репорт (поиск по нику)", anonymous="Скрыть ваш ник в канале репортов"
    )
    async def report(self, interaction: discord.Interaction, player: str, anonymous: bool = False):
        season = await season_or_first(self.seasons)
        target = await self.repo.player(player, season)
        if not target:
            await interaction.response.send_message("❌ Игрок не найден.", ephemeral=True)
            return
        name = service.target_name(target, player)
        await interaction.response.send_message(
            f"Репорт на **{name}**. Выберите причину(ы):",
            view=ReportReasonView(self.repo, player, name, anonymous, self.log_channel_id),
            ephemeral=True,
        )

    @report.autocomplete('player')
    async def report_player_autocomplete(self, interaction: discord.Interaction, current: str):
        try:
            season = await season_or_first(self.seasons)
            players = await self.repo.search(season, current)
            return [
                app_commands.Choice(
                    name=p.get("changed_nickname") or p.get("current_name", "Unknown"), value=p.get("steam_id", "")
                )
                for p in players
            ]
        except Exception:
            logger.exception("[AUTOCOMPLETE ERROR] /report")
            return []


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(
        ReportsCog(
            bot,
            ReportsRepo(bot.db.player_reports, bot.db.player_ratings),
            seasons_repo(bot.db),
            settings.reports_log_channel_id,
        )
    )
