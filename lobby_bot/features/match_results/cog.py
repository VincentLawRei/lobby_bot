"""Итоги матчей в канале MATCH_RESULTS_CHANNEL_ID: рейтинговые (изменения ELO, штрафы, твинки)
и нерейтинговые, статистика урона, меню детальной статистики игрока, репорт на игрока из матча.

Слушает события: `elo_matches_processed` (фича elo, список (match_uuid, season)) и
`casual_matches_imported` (фича match_stats, список match_uuid). Меню статистики и кнопка репорта
persistent (DynamicItem): работают на старых сообщениях после рестарта; репозитории им даёт MatchResultsCog
(после рестарта Discord создаёт их сам, и они берут Cog у interaction.client).
"""

import logging
import re

import discord
from discord.ext import commands

from lobby_bot.features.reports.repo import ReportsRepo
from lobby_bot.features.reports.service import REPORT_REASONS
from lobby_bot.settings import settings

from . import service
from .repo import MatchResultsRepo
from .service import CasualMatch, RankedMatch

logger = logging.getLogger(__name__)


# ============ Embed'ы ============
def add_damage_field(
    embed: discord.Embed, players_dmg: dict, raw_damage: dict, crew: list, traitors: list, player_names: dict
) -> None:
    """Поле со статистикой урона, если данные есть и embed не упирается в лимит 6000 символов."""
    damage_text = service.format_damage_stats(players_dmg, raw_damage, crew, traitors, player_names)
    if not damage_text:
        return

    used = len(embed.title or '') + len(embed.description or '') + len(embed.footer.text if embed.footer else '')
    used += sum(len(f.name) + len(f.value) for f in embed.fields)
    field_name = '📊 Статистика урона'
    available = 6000 - used - len(field_name) - 10
    if available > 50:
        if len(damage_text) > available:
            damage_text = damage_text[: available - 3] + '...'
        embed.add_field(name=field_name, value=damage_text, inline=False)


def _result_label(won: bool) -> str:
    return '✅ победа' if won else '❌ поражение'


def _sign(change: int) -> str:
    return '+' if change >= 0 else ''


def _date(dt) -> str:
    return dt.strftime('%d.%m.%Y %H:%M') if dt else '—'


def build_ranked_embed(m: RankedMatch) -> discord.Embed:
    def team_lines(players: list[dict]) -> str:
        lines = []
        for p in players:
            name = m.player_names.get(p['steam_id'], p['steam_id'])
            line = f"• **{name}**: {p['elo_before']} → {p['elo_after']} ({_sign(p['elo_change'])}{p['elo_change']})"
            penalty = m.penalties.get(p['steam_id'])
            if penalty:
                line += (
                    f" ⚠️ штраф -{service.penalty_effect(penalty, True)}%"
                    if p['won']
                    else f" ⚠️ штраф +{service.penalty_effect(penalty, False)}%"
                )
            lines.append(line)
        return '\n'.join(lines)

    embed = discord.Embed(
        title='⚔️ Ranked матч завершён',
        description=service.duration_text(m.duration_seconds),
        color=discord.Color.green() if m.crew_won else discord.Color.red(),
        timestamp=service.now_utc(),
    )
    embed.add_field(name=f'👥 Crew — {_result_label(m.crew_won)}', value=team_lines(m.crew) or '—', inline=False)
    embed.add_field(
        name=f'👹 Traitors — {_result_label(not m.crew_won)}', value=team_lines(m.traitors) or '—', inline=False
    )
    add_damage_field(embed, m.players_dmg, m.raw_damage, m.crew, m.traitors, m.player_names)
    embed.set_footer(text=f'Матч от {_date(m.match_datetime)} • {service.short_uuid(m.match_uuid)}')
    return embed


def build_penalty_embed(m: RankedMatch) -> discord.Embed | None:
    """Штрафные игроки матча для лог-канала штрафов."""
    lines = []
    for r in m.records:
        penalty = m.penalties.get(r['steam_id'])
        if not penalty:
            continue
        name = m.player_names.get(r['steam_id'], r['steam_id'])
        effect = (
            f"-{service.penalty_effect(penalty, True)}% к получению"
            if r['won']
            else f"+{service.penalty_effect(penalty, False)}% к потерям"
        )
        lines.append(
            f"• **{name}**: {r['elo_before']} → {r['elo_after']} ({_sign(r['elo_change'])}{r['elo_change']})"
            f" | {effect} | причина: {penalty.reason or '—'}"
        )
    if not lines:
        return None
    embed = discord.Embed(
        title="⚠️ Штрафные игроки в матче",
        description='\n'.join(lines),
        color=discord.Color.orange(),
        timestamp=service.now_utc(),
    )
    embed.set_footer(text=f'Матч {service.short_uuid(m.match_uuid)}')
    return embed


def build_twink_embed(m: RankedMatch) -> discord.Embed | None:
    """Игра на твинках в матче для лог-канала штрафов."""
    lines = []
    for t in m.twink_rows:
        twink_name = m.player_names.get(t['steam_id'], t['steam_id'])
        main_sid = t['twink_redirected_to']
        main_name = m.player_names.get(main_sid, main_sid)
        h = m.twink_main_changes.get(main_sid)
        if h:
            elo_text = f"{h['elo_before']} → {h['elo_after']} ({_sign(h['elo_change'])}{h['elo_change']})"
        else:
            elo_text = 'без изменений (основной тоже был в матче)'
        lines.append(f"• **{twink_name}** играл на твинке основного **{main_name}**: {elo_text}")
    if not lines:
        return None
    embed = discord.Embed(
        title="🔁 Обнаружена игра на твинке",
        description='\n'.join(lines),
        color=discord.Color.blurple(),
        timestamp=service.now_utc(),
    )
    embed.set_footer(text=f'Матч {service.short_uuid(m.match_uuid)}')
    return embed


def build_casual_embed(m: CasualMatch) -> discord.Embed:
    def team_lines(players: list[dict]) -> str:
        lines = []
        for p in players:
            role = p.get('role', '')
            lines.append(f"• **{m.player_names.get(p['steam_id'], p['steam_id'])}**{f' ({role})' if role else ''}")
        return '\n'.join(lines)

    embed = discord.Embed(
        title='🎮 Нерейтинговый матч завершён',
        description=service.duration_text(m.duration_seconds),
        color=discord.Color.green() if m.crew_won else discord.Color.red(),
        timestamp=service.now_utc(),
    )
    embed.add_field(name=f'👥 Crew — {_result_label(m.crew_won)}', value=team_lines(m.crew) or '—', inline=False)
    embed.add_field(
        name=f'👹 Traitors — {_result_label(not m.crew_won)}', value=team_lines(m.traitors) or '—', inline=False
    )
    add_damage_field(embed, m.players_dmg, m.raw_damage, m.crew, m.traitors, m.player_names)
    embed.set_footer(text=f'Матч от {_date(m.match_datetime)} • {service.short_uuid(m.match_uuid)}')
    return embed


# ============ Меню статистики и репорт ============
class PlayerSelectMenu(discord.ui.DynamicItem[discord.ui.Select], template=r'stats:(?P<match_id>[^:]+)'):
    """Детальная статистика выбранного игрока (persistent)."""

    def __init__(self, match_id: str, repo: MatchResultsRepo, player_options: list | None = None):
        self.match_id = match_id
        self.repo = repo
        options = [discord.SelectOption(label=label, value=steam_id) for steam_id, label in (player_options or [])]
        super().__init__(
            discord.ui.Select(
                custom_id=f'stats:{match_id}',
                placeholder='Выберите игрока...',
                options=options or [discord.SelectOption(label='—', value='none')],
                min_values=1,
                max_values=1,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select, match: re.Match):
        return cls(match.group('match_id'), _cog(interaction).repo)

    async def callback(self, interaction: discord.Interaction):
        steam_id = self.item.values[0]
        if steam_id == 'none':
            await interaction.response.send_message('❌ Нет данных.', ephemeral=True)
            return
        detail = await service.player_detail(self.repo, self.match_id, steam_id)
        if detail is None:
            await interaction.response.send_message('❌ Данные урона не найдены.', ephemeral=True)
            return
        await interaction.response.send_message(detail, ephemeral=True)


class ReportPlayerSelect(discord.ui.Select):
    def __init__(self, player_options: list):
        options = [discord.SelectOption(label=label[:100], value=steam_id) for steam_id, label in player_options[:25]]
        super().__init__(placeholder='Выберите игрока...', min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_player = self.values[0]
        self.view.selected_player_label = next(
            (o.label for o in self.options if o.value == self.values[0]), self.values[0]
        )
        await self.view.refresh(interaction)


class ReportReasonSelectMulti(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=r) for r in REPORT_REASONS]
        super().__init__(placeholder='Выберите причину(ы)...', min_values=1, max_values=len(options), options=options)

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_reasons = self.values
        await self.view.refresh(interaction)


class ReportAnonymousToggle(discord.ui.Button):
    def __init__(self):
        super().__init__(label='Анонимно: Нет', style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        self.view.anonymous = not self.view.anonymous
        self.label = f"Анонимно: {'Да' if self.view.anonymous else 'Нет'}"
        await self.view.refresh(interaction)


class ReportSubmitButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label='✅ Отправить репорт', style=discord.ButtonStyle.success, row=3)

    async def callback(self, interaction: discord.Interaction):
        view: ReportMatchView = self.view
        if not view.selected_player:
            await interaction.response.send_message('❌ Выберите игрока.', ephemeral=True)
            return
        if not view.selected_reasons:
            await interaction.response.send_message('❌ Выберите причину(ы).', ephemeral=True)
            return

        await service.save_report(
            view.reports_repo,
            str(interaction.user.id),
            interaction.user.display_name,
            view.anonymous,
            view.selected_player,
            view.selected_player_label,
            view.selected_reasons,
            view.match_id,
        )

        embed = discord.Embed(title='🚩 Новый репорт', color=discord.Color.red())
        embed.add_field(name='На игрока', value=f'**{view.selected_player_label}**', inline=True)
        embed.add_field(name='От', value='Аноним' if view.anonymous else interaction.user.mention, inline=True)
        embed.add_field(name='Причины', value='\n'.join(f'• {r}' for r in view.selected_reasons), inline=False)
        embed.timestamp = service.now_utc()

        log_channel = interaction.client.get_channel(settings.reports_log_channel_id)
        if log_channel:
            await log_channel.send(embed=embed)
        await interaction.response.edit_message(content='✅ Репорт отправлен. Спасибо!', view=None)


class ReportMatchView(discord.ui.View):
    def __init__(self, match_id: str, player_options: list, reports_repo: ReportsRepo):
        super().__init__(timeout=300)
        self.match_id = match_id
        self.reports_repo = reports_repo
        self.selected_player = None
        self.selected_player_label = None
        self.selected_reasons = []
        self.anonymous = False
        self.add_item(ReportPlayerSelect(player_options))
        self.add_item(ReportReasonSelectMulti())
        self.add_item(ReportAnonymousToggle())
        self.add_item(ReportSubmitButton())

    def build_content(self) -> str:
        player = self.selected_player_label or "— не выбран"
        reasons = "\n".join(f"• {r}" for r in self.selected_reasons) if self.selected_reasons else "— не выбраны"
        return (
            "## 🚩 Репорт на игрока\n\n"
            f"**Игрок:** {player}\n\n"
            f"**Причины:**\n{reasons}\n\n"
            f"**Анонимно:** {'Да' if self.anonymous else 'Нет'}"
        )

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content=self.build_content(), view=self)


class ReportMatchButton(discord.ui.DynamicItem[discord.ui.Button], template=r'report:(?P<match_id>[^:]+)'):
    """Кнопка репорта на игрока из матча (persistent)."""

    def __init__(self, match_id: str, repo: MatchResultsRepo, reports_repo: ReportsRepo):
        self.match_id = match_id
        self.repo = repo
        self.reports_repo = reports_repo
        super().__init__(
            discord.ui.Button(custom_id=f'report:{match_id}', label='🚩 Репорт', style=discord.ButtonStyle.danger)
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match):
        cog = _cog(interaction)
        return cls(match.group('match_id'), cog.repo, cog.reports_repo)

    async def callback(self, interaction: discord.Interaction):
        player_options = await service.report_player_options(self.repo, self.match_id)
        if not player_options:
            await interaction.response.send_message('❌ Игроки матча не найдены.', ephemeral=True)
            return
        view = ReportMatchView(self.match_id, player_options, self.reports_repo)
        await interaction.response.send_message(content=view.build_content(), view=view, ephemeral=True)


class DetailedStatsView(discord.ui.View):
    """Под сообщением об итогах: меню статистики игроков, репорт, ссылка на реплей."""

    def __init__(
        self,
        match_id: str,
        players_dmg: dict,
        player_names: dict,
        site_url: str,
        repo: MatchResultsRepo,
        reports_repo: ReportsRepo,
    ):
        super().__init__(timeout=None)
        self.match_id = match_id
        options = service.stats_menu_options(players_dmg, player_names)
        if options:
            self.add_item(PlayerSelectMenu(match_id, repo, options))
        self.add_item(ReportMatchButton(match_id, repo, reports_repo))
        self.add_item(
            discord.ui.Button(
                label='🎬 Открыть реплей', style=discord.ButtonStyle.link, url=f'{site_url}/map/?m={match_id}'
            )
        )


# ============ Cog ============
def _cog(interaction: discord.Interaction) -> "MatchResultsCog":
    """Cog фичи: из него persistent-элементы, созданные Discord после рестарта, берут репозитории."""
    return interaction.client.get_cog(MatchResultsCog.__cog_name__)


class MatchResultsCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        repo: MatchResultsRepo,
        reports_repo: ReportsRepo,
        results_channel_id: int,
        penalty_channel_id: int,
        site_url: str,
    ) -> None:
        self.bot = bot
        self.repo = repo
        self.reports_repo = reports_repo
        self.results_channel_id = results_channel_id
        self.penalty_channel_id = penalty_channel_id
        self.site_url = site_url

    @commands.Cog.listener()
    async def on_elo_matches_processed(self, matches: list[tuple[str, int]]) -> None:
        for match_uuid, season in matches:  # по порядку: итоги матчей в канале идут в порядке игр
            try:
                await self.send_ranked(match_uuid, season)
            except Exception:
                logger.exception("Не удалось отправить итоги рейтингового матча %s", match_uuid)

    @commands.Cog.listener()
    async def on_casual_matches_imported(self, match_uuids: list[str]) -> None:
        for match_uuid in match_uuids:
            try:
                await self.send_casual(match_uuid)
            except Exception:
                logger.exception("Не удалось отправить итоги нерейтингового матча %s", match_uuid)

    def _results_channel(self):
        channel = self.bot.get_channel(self.results_channel_id)
        if not channel:
            logger.warning('⚠️ Канал уведомлений %s не найден!', self.results_channel_id)
        return channel

    async def send_ranked(self, match_uuid: str, season: int) -> None:
        channel = self._results_channel()
        if not channel:
            return
        m = await service.ranked_match(self.repo, match_uuid, season)
        if not m:
            return
        await channel.send(embed=build_ranked_embed(m), view=self._stats_view(match_uuid, m))

        penalty_channel = self.bot.get_channel(self.penalty_channel_id)
        if not penalty_channel:
            return
        for extra in (build_penalty_embed(m) if m.penalties else None, build_twink_embed(m) if m.twink_rows else None):
            if extra:
                try:
                    await penalty_channel.send(embed=extra)
                except Exception:
                    logger.exception("Не удалось отправить в лог штрафов (матч %s)", match_uuid)

    async def send_casual(self, match_uuid: str) -> None:
        channel = self._results_channel()
        if not channel:
            return
        m = await service.casual_match(self.repo, match_uuid)
        if not m:
            return
        await channel.send(embed=build_casual_embed(m), view=self._stats_view(match_uuid, m))

    def _stats_view(self, match_uuid: str, m: RankedMatch | CasualMatch) -> DetailedStatsView:
        return DetailedStatsView(match_uuid, m.players_dmg, m.player_names, self.site_url, self.repo, self.reports_repo)


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(PlayerSelectMenu, ReportMatchButton)  # persistent: старые сообщения работают после рестарта
    db = bot.db
    repo = MatchResultsRepo(db.history_player_stats, db.player_ratings, db.elo_history, db.match_damage)
    reports_repo = ReportsRepo(db.player_reports, db.player_ratings)
    await bot.add_cog(
        MatchResultsCog(
            bot,
            repo,
            reports_repo,
            settings.match_results_channel_id,
            settings.penalty_log_channel_id,
            settings.site_url,
        )
    )
