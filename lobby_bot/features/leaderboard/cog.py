"""Лидерборд ELO: сообщения в каналах (таблица и статистика Traitors), команды и кнопки таблицы.

Сообщения в каналах обновляются раз в 5 минут и сразу после расчёта новых матчей
(событие `elo_matches_processed` от фичи elo). Данные и расчёты в service.py.
"""

import logging

import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
from rapidfuzz import fuzz, process

from lobby_bot.features.seasons.service import seasons_repo
from lobby_bot.settings import settings

from . import service
from .repo import LeaderboardRepo
from .service import (
    MATCHES_PAGE_SIZE,
    MIN_PAIR_GAMES,
    MIN_TRAITORS_GAMES,
    PAGE_SIZE,
    STATIC_TOP,
    clip,
    get_display_name,
    total_pages,
)

logger = logging.getLogger(__name__)

PLAYER_COMMAND_HINT = '🔍 </player:100000000000000001> — найти игрока'
LEGEND = '(5/10) калибровка | 👥 Crew | 👹 Thr | `9ч` — последний матч'
RATING_NOTE = (
    '📌 *Пояснения по рейтингу: в силу того, что зачастую люди играют в разных или даже изолированных группах, '
    'рейтинговая таблица показывает скорее относительный рейтинг в рамках своих конкретных групп. Пересечения, '
    'т.е. anchor-игроки, играющие в разных группах, теоретически могут балансировать перелив эло-рейтинга, но '
    'только в том случае, если они часто и постоянно играют в разных группах.*'
)


def _stamp() -> str:
    return service.now_utc().strftime("%d.%m.%Y %H:%M")


# ============ Embed'ы ============
async def build_static_leaderboard_embed(repo: LeaderboardRepo, players: list[dict]) -> discord.Embed:
    """Сообщение в канале лидерборда: топ STATIC_TOP и итоги сезона."""
    now = service.now_utc()
    lines = [service.build_player_line(i + 1, p, now) for i, p in enumerate(players[:STATIC_TOP])]
    description = '\n'.join(lines) if lines else 'Нет данных. Сыграйте ranked матчи!'
    if len(players) > STATIC_TOP:
        description += f'\n\n*...и ещё {len(players) - STATIC_TOP} игроков*'
    description += '\n\n⚠️ *Игрок получил штраф*'
    description += '\n\n:skull: *Раз в 7 дней снимает эло у инактива*'
    description += '\n\n' + RATING_NOTE
    description += '\n' + PLAYER_COMMAND_HINT

    season = await service.season_now(repo)
    embed = discord.Embed(title=f'🏆 ELO Таблица — Сезон {season}', description=description, color=discord.Color.gold())
    stats = await service.get_win_rate_stats(repo, season)
    embed.set_footer(
        text=(
            f'Обновлено: {_stamp()} UTC • {LEGEND}\n'
            f'Всего ранговых матчей: {stats["total"]} • 👥 Победа мирных {stats["crew_wr"]}% | '
            f'👹 Победа предателей {stats["traitors_wr"]}%'
        )
    )
    return embed


async def build_leaderboard_page_embed(
    repo: LeaderboardRepo, players: list[dict], page: int, season: int
) -> discord.Embed:
    """Страница таблицы (ephemeral). Прошлый сезон помечается как архив."""
    start = page * PAGE_SIZE
    now = service.now_utc()
    lines = [service.build_player_line(start + i + 1, p, now) for i, p in enumerate(players[start : start + PAGE_SIZE])]

    is_past = season != await service.season_now(repo)
    embed = discord.Embed(
        title=f'🏆 ELO Таблица — Сезон {season}' + (' (архив)' if is_past else ''),
        description='\n'.join(lines) if lines else 'Нет данных.',
        color=discord.Color.greyple() if is_past else discord.Color.gold(),
    )
    if not is_past:
        embed.description += '\n\n' + PLAYER_COMMAND_HINT
    embed.set_footer(text=f'Стр. {page + 1}/{total_pages(len(players), PAGE_SIZE)} • {LEGEND}')
    return embed


def _section(title: str, rows: list[dict], line, medals: bool = True) -> str:
    lines = [title]
    for i, row in enumerate(rows):
        place = service.medal(i + 1, f'`#{i + 1}`') if medals else f'`#{i + 1}`'
        lines.append(f'{place} {line(row)}')
    return '\n'.join(lines)


def build_duos_embed(season: int, blocks: dict[str, list[dict]]) -> discord.Embed:
    """Статистика Traitors: дуэты, топ WR, текущие серии, частые предатели и др. Пустые блоки не показываются."""

    def pair_line(rate):
        return lambda p: (
            f"**{p['name1']}** + **{p['name2']}** — {rate(p)} | {p['wins']}W/{p['losses']}L ({p['games']} игр)"
        )

    def wr_line(p):
        return f"**{p['name']}** — {round(p['win_rate'] * 100)}% WR | {p['wins']}W/{p['losses']}L ({p['games']} игр)"

    def streak_line(word):
        return lambda p: f"**{p['name']}** — {p['streak']} {word} подряд"

    sections = []
    top_title = f'**Топ дуэты (мин. {MIN_PAIR_GAMES} игр)**'
    if blocks['top_pairs']:
        sections.append(
            _section(top_title, blocks['top_pairs'], pair_line(lambda p: f"{round(p['win_rate'] * 100)}% WR"))
        )
    else:
        sections.append(top_title + '\nНедостаточно данных')

    optional = [
        (
            'anti_pairs',
            f'**Антидуэты (мин. {MIN_PAIR_GAMES} игр)**',
            pair_line(lambda p: f"{round((1 - p['win_rate']) * 100)}% LR"),
            True,
        ),
        ('traitors_wr', f'**Топ-10 WR за Traitors (мин. {MIN_TRAITORS_GAMES} игр, 3 weeks)**', wr_line, True),
        ('crew_wr', f'**Топ-10 WR за мирных (мин. {MIN_TRAITORS_GAMES} игр, 3 weeks)**', wr_line, True),
        ('traitors_win_streaks', '**Текущие серии побед за Traitors (7 дней)**', streak_line('побед'), True),
        ('crew_win_streaks', '**Текущие серии побед за Crew (7 дней)**', streak_line('побед'), True),
        ('traitors_loss_streaks', '**Текущие серии поражений за Traitors (7 дней)**', streak_line('поражений'), True),
        ('crew_loss_streaks', '**Текущие серии поражений за мирных (7 дней)**', streak_line('поражений'), True),
        (
            'frequent_traitors',
            '**Самые частые предатели**',
            lambda p: (
                f"**{p['name']}** — {p['traitors_games']}/{p['total_games']} игр "
                f"({round(p['traitors_games'] / p['total_games'] * 100)}% предателем)"
            ),
            True,
        ),
        (
            'eternal_civilians',
            '**Вечный мирный**',
            lambda p: (
                f"**{p['name']}** — {round((1 - p['traitors_rate']) * 100)}% мирным "
                f"({p['total_games'] - p['traitors_games']}/{p['total_games']} игр)"
            ),
            False,
        ),
        (
            'double_dead',
            '**Double Dead King 💀**',
            lambda p: f"**{p['name']}** — {p['count']} матчей с 2+ смертями",
            True,
        ),
        (
            'recent_traitors_3d',
            '**Самый частый предатель за последние 3 дня**',
            lambda p: f"**{p['name']}** — {p['count']} игр за Traitors",
            False,
        ),
        (
            'recent_traitors_7d',
            '**Самый частый предатель за последние 7 дней**',
            lambda p: f"**{p['name']}** — {p['count']} игр за Traitors",
            False,
        ),
    ]
    for key, title, line, medals in optional:
        if blocks[key]:
            sections.append(_section(title, blocks[key], line, medals))

    embed = discord.Embed(
        title=f'👹 Traitors Stats — Сезон {season}', description='\n\n'.join(sections), color=discord.Color.dark_red()
    )
    embed.set_footer(text=f'Обновлено: {service.now_utc().strftime("%d.%m.%Y %H:%M")} UTC')
    return embed


async def get_duos_embed(repo: LeaderboardRepo, season: int | None = None) -> discord.Embed:
    if season is None:
        season = await service.season_now(repo)
    return build_duos_embed(season, await service.traitors_stats(repo, season))


async def build_player_card(
    repo: LeaderboardRepo, player: dict, rank: int, season: int | None = None
) -> tuple[discord.Embed, discord.File | None]:
    """Карточка игрока за сезон season (+1 к просмотрам). Возвращает embed и PNG с графиком ELO."""
    if season is None:
        season = await service.season_now(repo)
    steam_id = player.get('steam_id')
    views = await repo.add_profile_view(steam_id)

    name = get_display_name(player)
    elo = player.get('elo', 1500)
    wins, losses = player.get('wins', 0), player.get('losses', 0)
    ranked_games = player.get('ranked_games', 0)
    wr = round(player.get('win_rate', 0) * 100)
    is_calibrated = player.get('is_calibrated', False)
    s_games, s_wins = player.get('crew_games', 0), player.get('crew_wins', 0)
    t_games, t_wins = player.get('traitors_games', 0), player.get('traitors_wins', 0)
    s_wr = round(s_wins / s_games * 100) if s_games else 0
    t_wr = round(t_wins / t_games * 100) if t_games else 0

    status = (
        '✅ Откалиброван'
        if is_calibrated
        else f'🔄 Калибровка ({player.get("calibration_games", 0)}/{service.CALIBRATION_GAMES})'
    )
    if player.get('is_penalized'):
        status += ' ⚠️ Штраф'

    embed = discord.Embed(
        title=f'{service.medal(rank, f"#{rank}")} {name}',
        color=discord.Color.gold() if is_calibrated else discord.Color.greyple(),
    )
    embed.add_field(name='ELO', value=f'**{elo}**', inline=True)
    embed.add_field(name='Место', value=f'#{rank}', inline=True)
    embed.add_field(name='Статус', value=status, inline=True)
    embed.add_field(
        name='Общая статистика', value=f'{wins}W / {losses}L — **{wr}%** WR ({ranked_games} игр)', inline=False
    )
    embed.add_field(
        name='👥 Crew',
        inline=True,
        value=(f'{s_wins}W / {s_games - s_wins}L — **{s_wr}%** WR ({s_games} игр)' if s_games else 'Нет игр'),
    )
    embed.add_field(
        name='👹 Traitors',
        inline=True,
        value=(f'{t_wins}W / {t_games - t_wins}L — **{t_wr}%** WR ({t_games} игр)' if t_games else 'Нет игр'),
    )
    traitors_rate = round(t_games / ranked_games * 100) if ranked_games else 0
    embed.add_field(
        name='🎲 Выпадение предателем', value=f'**{traitors_rate}%** ({t_games}/{ranked_games} игр)', inline=False
    )
    embed.add_field(name='👁 Просмотры карточки', value=f'**{views}**', inline=False)

    recent = await repo.recent_matches(steam_id, season, skip=0, limit=MATCHES_PAGE_SIZE)
    if recent:
        embed.add_field(
            name='🕓 Последние матчи',
            inline=False,
            value=clip('\n'.join(await service.match_lines(repo, steam_id, recent))),
        )

    past = await service.past_season_placements(repo, steam_id, season)
    if past:
        lines = [
            f'**Сезон {p["season"]}:** {service.medal(p["rank"], "#" + str(p["rank"]))} · **{p["elo"]}** ELO'
            for p in past
        ]
        embed.add_field(name='📜 Прошлые сезоны', value=clip('\n'.join(lines)), inline=False)

    embed.set_footer(text=f'Сезон {season} • {_stamp()} UTC')

    chart = await service.generate_elo_chart(repo, steam_id, season)
    if not chart:
        return embed, None
    embed.set_image(url='attachment://elo_chart.png')
    return embed, discord.File(chart, filename='elo_chart.png')


async def build_match_history_embed(
    repo: LeaderboardRepo, steam_id: str, name: str, page: int, season: int
) -> discord.Embed:
    records = await repo.recent_matches(steam_id, season, skip=page * MATCHES_PAGE_SIZE, limit=MATCHES_PAGE_SIZE)
    pages = total_pages(await repo.matches_count(steam_id, season), MATCHES_PAGE_SIZE)
    embed = discord.Embed(title=f'🕓 История матчей — {name}', color=discord.Color.blurple())
    lines = await service.match_lines(repo, steam_id, records)
    embed.description = clip('\n'.join(lines)) if records else 'Нет матчей.'
    embed.set_footer(text=f'Стр. {page + 1}/{pages} • Сезон {season}')
    return embed


async def send_player_card(
    interaction: discord.Interaction,
    repo: LeaderboardRepo,
    player: dict,
    rank: int,
    season: int,
    footer: str | None = None,
) -> None:
    embed, file = await build_player_card(repo, player, rank, season)
    if footer:
        embed.set_footer(text=footer)
    kwargs = {
        'embed': embed,
        'ephemeral': True,
        'view': PlayerCardView(repo, player.get('steam_id'), get_display_name(player), season),
    }
    if file:
        kwargs['file'] = file
    await interaction.response.send_message(**kwargs)


# ============ Кнопки и меню ============
class MatchHistoryView(ui.View):
    def __init__(self, repo: LeaderboardRepo, steam_id: str, name: str, season: int, pages: int, page: int = 0):
        super().__init__(timeout=300)
        self.repo, self.steam_id, self.name, self.season, self.page = repo, steam_id, name, season, page
        self.total_pages = pages
        self._build()

    @classmethod
    async def create(cls, repo: LeaderboardRepo, steam_id: str, name: str, season: int) -> 'MatchHistoryView':
        # Как раньше: страницы кнопок считаются вместе с начислениями за твинка
        count = await repo.matches_count(steam_id, season, with_twink=True)
        return cls(repo, steam_id, name, season, total_pages(count, MATCHES_PAGE_SIZE))

    def _build(self):
        self.clear_items()
        prev = ui.Button(label='◀', style=discord.ButtonStyle.secondary, disabled=self.page == 0)
        prev.callback = self.prev_page
        self.add_item(prev)
        self.add_item(
            ui.Button(label=f'{self.page + 1} / {self.total_pages}', style=discord.ButtonStyle.primary, disabled=True)
        )
        nxt = ui.Button(label='▶', style=discord.ButtonStyle.secondary, disabled=self.page >= self.total_pages - 1)
        nxt.callback = self.next_page
        self.add_item(nxt)

    async def _show(self, interaction: discord.Interaction, page: int):
        self.page = page
        self._build()
        embed = await build_match_history_embed(self.repo, self.steam_id, self.name, self.page, self.season)
        await interaction.response.edit_message(embed=embed, view=self)

    async def prev_page(self, interaction: discord.Interaction):
        await self._show(interaction, self.page - 1)

    async def next_page(self, interaction: discord.Interaction):
        await self._show(interaction, self.page + 1)


class PlayerCardView(ui.View):
    def __init__(self, repo: LeaderboardRepo, steam_id: str, name: str, season: int):
        super().__init__(timeout=300)
        self.repo, self.steam_id, self.name, self.season = repo, steam_id, name, season

    @ui.button(label='🕓 История матчей', style=discord.ButtonStyle.secondary)
    async def show_history(self, interaction: discord.Interaction, button: ui.Button):
        embed = await build_match_history_embed(self.repo, self.steam_id, self.name, 0, self.season)
        view = await MatchHistoryView.create(self.repo, self.steam_id, self.name, self.season)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class PlayerSelectMenu(ui.Select):
    """Выбор игрока на странице таблицы: открывает карточку."""

    def __init__(self, repo: LeaderboardRepo, players: list[dict], page: int, season: int):
        self.repo, self.season = repo, season
        start = page * PAGE_SIZE
        options = []
        for i, p in enumerate(players[start : start + PAGE_SIZE]):
            rank = start + i + 1
            label = f'{service.medal(rank, "")}#{rank} {get_display_name(p)}'[:100]
            options.append(
                discord.SelectOption(label=label, description=f'{p.get("elo", 1500)} ELO', value=str(rank - 1))
            )
        super().__init__(placeholder='👤 Карточка игрока...', options=options, custom_id='lb_player_select', row=1)

    async def callback(self, interaction: discord.Interaction):
        players = await service.fetch_all_players(self.repo, self.season)
        idx = int(self.values[0])
        if idx >= len(players):
            await interaction.response.send_message('❌ Игрок не найден.', ephemeral=True)
            return
        await send_player_card(interaction, self.repo, players[idx], idx + 1, self.season)


class LeaderboardView(ui.View):
    """Ephemeral таблица: страницы и выбор игрока."""

    def __init__(self, repo: LeaderboardRepo, players: list[dict], season: int, page: int = 0):
        super().__init__(timeout=300)
        self.repo, self.players, self.season, self.page = repo, players, season, page
        self._build()

    def _build(self):
        self.total_pages = total_pages(len(self.players), PAGE_SIZE)
        self.clear_items()
        prev = ui.Button(label='◀', style=discord.ButtonStyle.secondary, disabled=self.page == 0, row=0)
        prev.callback = self.prev_page
        self.add_item(prev)
        self.add_item(
            ui.Button(
                label=f'{self.page + 1} / {self.total_pages}', style=discord.ButtonStyle.primary, disabled=True, row=0
            )
        )
        nxt = ui.Button(
            label='▶', style=discord.ButtonStyle.secondary, disabled=self.page >= self.total_pages - 1, row=0
        )
        nxt.callback = self.next_page
        self.add_item(nxt)
        self.add_item(PlayerSelectMenu(self.repo, self.players, self.page, self.season))

    async def _show(self, interaction: discord.Interaction, page: int):
        self.players = await service.fetch_all_players(self.repo, self.season)
        self.page = page
        self._build()
        await interaction.response.edit_message(
            embed=await build_leaderboard_page_embed(self.repo, self.players, self.page, self.season), view=self
        )

    async def prev_page(self, interaction: discord.Interaction):
        await self._show(interaction, self.page - 1)

    async def next_page(self, interaction: discord.Interaction):
        await self._show(interaction, self.page + 1)


async def send_leaderboard(
    interaction: discord.Interaction, repo: LeaderboardRepo, season: int, title: str | None = None
) -> None:
    players = await service.fetch_all_players(repo, season)
    embed = await build_leaderboard_page_embed(repo, players, page=0, season=season)
    if title:
        embed.title = title
    await interaction.response.send_message(embed=embed, view=LeaderboardView(repo, players, season), ephemeral=True)


class SeasonSelectMenu(ui.Select):
    def __init__(self, repo: LeaderboardRepo, seasons: list[int]):
        self.repo = repo
        options = [discord.SelectOption(label=f'Сезон {s}', value=str(s), emoji='🗓') for s in seasons[:25]]
        super().__init__(placeholder='📜 Выберите сезон...', options=options, custom_id='lb_season_select')

    async def callback(self, interaction: discord.Interaction):
        season = int(self.values[0])
        players = await service.fetch_all_players(self.repo, season)
        if not players:
            await interaction.response.send_message(f'❌ Нет данных за сезон {season}.', ephemeral=True)
            return
        await interaction.response.send_message(
            embed=await build_leaderboard_page_embed(self.repo, players, 0, season),
            view=LeaderboardView(self.repo, players, season),
            ephemeral=True,
        )


class SeasonSelectView(ui.View):
    def __init__(self, repo: LeaderboardRepo, seasons: list[int]):
        super().__init__(timeout=300)
        self.add_item(SeasonSelectMenu(repo, seasons))


class FullTableButton(ui.View):
    """Кнопки под сообщением в канале (persistent: работают после рестарта)."""

    def __init__(self, repo: LeaderboardRepo):
        super().__init__(timeout=None)
        self.repo = repo

    @ui.button(label='📋 Полная таблица', style=discord.ButtonStyle.primary, custom_id='lb_open_full')
    async def open_full(self, interaction: discord.Interaction, button: ui.Button):
        await send_leaderboard(interaction, self.repo, await service.season_now(self.repo))

    @ui.button(label='📜 Прошлые сезоны', style=discord.ButtonStyle.secondary, custom_id='lb_past_seasons')
    async def past_seasons(self, interaction: discord.Interaction, button: ui.Button):
        seasons = await service.past_seasons(self.repo)
        if not seasons:
            await interaction.response.send_message('❌ Прошлых сезонов пока нет.', ephemeral=True)
            return
        await interaction.response.send_message(
            content='📜 **Прошлые сезоны** — выберите сезон для просмотра таблицы:',
            view=SeasonSelectView(self.repo, seasons),
            ephemeral=True,
        )


# ============ Поиск игрока ============
def find_player(players: list[dict], query: str) -> int | None:
    """Индекс игрока: номер из автокомплита или ближайший ник (не хуже 40 по WRatio)."""
    try:
        idx = int(query)
        players[idx]
        return idx
    except (ValueError, IndexError):
        pass
    result = process.extractOne(query, [get_display_name(p) for p in players], scorer=fuzz.WRatio)
    if not result or result[1] < 40:
        return None
    return result[2]


def player_choices(players: list[dict], current: str, cutoff: int) -> list[app_commands.Choice[str]]:
    """Автокомплит по никам: значение — индекс в таблице."""
    names = [get_display_name(p) for p in players]
    if not current:
        return [app_commands.Choice(name=f'#{i + 1} {n}', value=str(i)) for i, n in enumerate(names[:25])]
    results = process.extract(current, names, scorer=fuzz.partial_ratio, limit=25, score_cutoff=cutoff)
    return [app_commands.Choice(name=f'#{idx + 1} {n}', value=str(idx)) for n, _, idx in results]


# ============ Cog ============
class LeaderboardCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        repo: LeaderboardRepo,
        leaderboard_channel_id: int,
        duos_channel_id: int,
        moderator_role_id: int,
    ) -> None:
        self.bot = bot
        self.repo = repo
        self.leaderboard_channel_id = leaderboard_channel_id
        self.duos_channel_id = duos_channel_id
        self.moderator_role_id = moderator_role_id

    async def cog_load(self) -> None:
        self.leaderboard_update_task.start()

    async def cog_unload(self) -> None:
        self.leaderboard_update_task.cancel()

    # ---------- Сообщения в каналах ----------
    @tasks.loop(minutes=5)
    async def leaderboard_update_task(self) -> None:
        await self._safe_update()

    @leaderboard_update_task.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_elo_matches_processed(self, matches: list[tuple[str, int]]) -> None:
        await self._safe_update()

    async def _safe_update(self) -> None:
        try:
            await self.update_leaderboard()
        except Exception:
            logger.exception("Ошибка обновления лидерборда")

    async def update_leaderboard(self) -> None:
        channel = self.bot.get_channel(self.leaderboard_channel_id)
        if not channel:
            logger.warning('⚠️ Канал лидерборда %s не найден!', self.leaderboard_channel_id)
            return
        players = await service.fetch_all_players(self.repo)
        embed = await build_static_leaderboard_embed(self.repo, players)
        await self._publish(channel, service.KEY_LEADERBOARD, 'ELO Таблица', embed, FullTableButton(self.repo))

        duos_channel = self.bot.get_channel(self.duos_channel_id)
        if not duos_channel:
            logger.warning('⚠️ Канал дуэтов %s не найден!', self.duos_channel_id)
            return
        duos_embed = await get_duos_embed(self.repo)
        await self._publish(duos_channel, service.KEY_DUOS, 'Топ дуэты', duos_embed, None)

    async def _publish(self, channel, key: str, title_marker: str, embed: discord.Embed, view) -> None:
        """Отредактировать сохранённое сообщение бота; если его нет — найти по заголовку в истории или отправить."""
        kwargs = {'embed': embed} if view is None else {'embed': embed, 'view': view}
        message_id = await self.repo.message_id(key)
        if message_id:
            try:
                msg = await channel.fetch_message(message_id)
                await msg.edit(**kwargs)
            except discord.NotFound:
                await self.repo.save_message_id(key, None)
                msg = await channel.send(**kwargs)
                await self.repo.save_message_id(key, msg.id)
            return
        async for msg in channel.history(limit=20):
            if msg.author == self.bot.user and msg.embeds and title_marker in (msg.embeds[0].title or ''):
                await self.repo.save_message_id(key, msg.id)
                await msg.edit(**kwargs)
                return
        msg = await channel.send(**kwargs)
        await self.repo.save_message_id(key, msg.id)
        logger.info('✅ Сообщение %s создано, ID: %s', key, msg.id)

    # ---------- Команды ----------
    @app_commands.command(name='leaderboard', description='Открыть интерактивную ELO таблицу')
    async def leaderboard(self, interaction: discord.Interaction):
        await send_leaderboard(interaction, self.repo, await service.season_now(self.repo))

    @app_commands.command(name='player', description='Показать карточку игрока')
    @app_commands.describe(ник='Начните вводить никнейм')
    async def player(self, interaction: discord.Interaction, ник: str):
        season = await service.season_now(self.repo)
        players = await service.fetch_all_players(self.repo, season)
        idx = find_player(players, ник)
        if idx is None:
            await interaction.response.send_message(f'❌ Игрок **{ник}** не найден.', ephemeral=True)
            return
        await send_player_card(interaction, self.repo, players[idx], idx + 1, season)

    @player.autocomplete('ник')
    async def player_autocomplete(self, interaction: discord.Interaction, current: str):
        players = await service.fetch_all_players(self.repo)
        return player_choices(players, current, cutoff=60)

    @app_commands.command(
        name='change_rating_nickname', description='[Мод] Изменить отображаемый ник игрока в рейтинге'
    )
    @app_commands.describe(
        игрок='Начните вводить никнейм игрока', новый_ник='Новый отображаемый ник (оставьте пустым чтобы сбросить)'
    )
    async def change_rating_nickname(self, interaction: discord.Interaction, игрок: str, новый_ник: str = ''):
        # Как раньше: только роль модератора (без ADMIN_IDS)
        member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        if not member or not any(role.id == self.moderator_role_id for role in member.roles):
            await interaction.response.send_message('❌ Недостаточно прав. Требуется роль модератора.', ephemeral=True)
            return

        players = await service.fetch_all_players(self.repo)
        idx = find_player(players, игрок)
        if idx is None:
            await interaction.response.send_message(f'❌ Игрок **{игрок}** не найден.', ephemeral=True)
            return
        player = players[idx]
        steam_id = player.get('steam_id')
        old_name = get_display_name(player)
        nickname = новый_ник.strip()

        await self.repo.set_nickname(steam_id, nickname or None)
        if nickname:
            text = f'✅ Ник игрока **{old_name}** изменён на **{nickname}**\n`steam_id: {steam_id}`'
        else:
            text = (
                f'✅ Кастомный ник для **{old_name}** сброшен. Теперь отображается: '
                f'**{player.get("current_name", "Unknown")}**\n`steam_id: {steam_id}`'
            )
        await interaction.response.send_message(text, ephemeral=True)

    @change_rating_nickname.autocomplete('игрок')
    async def change_nickname_autocomplete(self, interaction: discord.Interaction, current: str):
        players = await service.fetch_all_players(self.repo)
        return player_choices(players, current, cutoff=40)

    @app_commands.command(name='season_leaderboard', description='Показать лидерборд прошлого сезона')
    @app_commands.describe(сезон='Номер сезона')
    async def season_leaderboard(self, interaction: discord.Interaction, сезон: int):
        current = await service.season_now(self.repo)
        players = await service.fetch_all_players(self.repo, сезон)
        if not players:
            await interaction.response.send_message(f'❌ Нет данных для сезона {сезон}.', ephemeral=True)
            return
        label = f'Сезон {сезон}' + (' (текущий)' if сезон == current else ' (архив)')
        embed = await build_leaderboard_page_embed(self.repo, players, page=0, season=сезон)
        embed.title = f'🏆 ELO Таблица — {label}'
        await interaction.response.send_message(
            embed=embed, view=LeaderboardView(self.repo, players, сезон), ephemeral=True
        )

    @app_commands.command(name='season_stats', description='Показать статистику Traitors за прошлый сезон')
    @app_commands.describe(сезон='Номер сезона')
    async def season_stats(self, interaction: discord.Interaction, сезон: int):
        players = await service.fetch_all_players(self.repo, сезон)
        if not players:
            await interaction.response.send_message(f'❌ Нет данных для сезона {сезон}.', ephemeral=True)
            return
        embed = await get_duos_embed(self.repo, сезон)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name='season_player', description='Показать карточку игрока за прошлый сезон')
    @app_commands.describe(ник='Никнейм игрока', сезон='Номер сезона')
    async def season_player(self, interaction: discord.Interaction, ник: str, сезон: int):
        players = await service.fetch_all_players(self.repo, сезон)
        if not players:
            await interaction.response.send_message(f'❌ Нет данных для сезона {сезон}.', ephemeral=True)
            return
        result = process.extractOne(ник, [get_display_name(p) for p in players], scorer=fuzz.WRatio)
        if not result or result[1] < 40:
            await interaction.response.send_message(f'❌ Игрок **{ник}** не найден в сезоне {сезон}.', ephemeral=True)
            return
        idx = result[2]
        await send_player_card(interaction, self.repo, players[idx], idx + 1, сезон, footer=f'Сезон {сезон} (архив)')


async def setup(bot: commands.Bot) -> None:
    db = bot.db
    repo = LeaderboardRepo(
        db.player_ratings, db.history_player_stats, db.elo_history, db.discord_panel, db.profile_views, seasons_repo(db)
    )
    bot.add_view(FullTableButton(repo))  # persistent-кнопки под сообщением в канале
    await bot.add_cog(
        LeaderboardCog(bot, repo, settings.leaderboard_channel_id, settings.duos_channel_id, settings.moderator_role_id)
    )
