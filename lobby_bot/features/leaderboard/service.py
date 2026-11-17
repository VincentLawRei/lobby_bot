"""Лидерборд: расчёты статистики, строки таблицы и график ELO (без discord).

Чистые функции (пары, стрики, винрейт, вечный мирный, форматирование) принимают строки из базы;
async-функции `*_stats` / `fetch_all_players` собирают данные через LeaderboardRepo (его передаёт вызывающий).
График рисует matplotlib: синхронно, в отдельном потоке.
"""

import asyncio
import io
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from itertools import combinations

import matplotlib

matplotlib.use('Agg')
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from lobby_bot.features.elo.service import CALIBRATION_GAMES
from lobby_bot.features.seasons.service import current_season

from .repo import LeaderboardRepo

logger = logging.getLogger(__name__)

PAGE_SIZE = 25  # игроков на странице в ephemeral таблице (лимит select menu)
STATIC_TOP = 15  # сколько показывать в сообщении канала
MATCHES_PAGE_SIZE = 8  # матчей на странице истории и в карточке
MIN_PAIR_GAMES = 3  # минимум совместных игр для топа пар
TOP_PAIRS_COUNT = 5
MIN_TRAITORS_GAMES = 6  # минимум игр за команду для топа WR
TEAM_WR_DAYS = 21  # окно топа WR
TRAITORS_AVG_RATE = 0.33  # средняя доля игр предателем (вечный мирный)
CIVILIAN_BAYES_K = 5
CIVILIAN_MIN_GAMES = 7
MEDALS = {1: '🥇', 2: '🥈', 3: '🥉'}

KEY_LEADERBOARD = 'leaderboard_message_id'
KEY_DUOS = 'duos_message_id'


async def season_now(repo: LeaderboardRepo) -> int:
    return await current_season(repo.seasons)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)  # в Mongo даты наивные в UTC


def medal(rank: int, fallback: str) -> str:
    return MEDALS.get(rank, fallback)


# ============ Имена ============
def get_display_name(player: dict) -> str:
    """Отображаемый ник (changed_nickname), иначе имя из игры."""
    return player.get('changed_nickname') or player.get('current_name', 'Unknown')


async def apply_changed_nicknames(repo: LeaderboardRepo, rows: list[dict], name_fields: list[str]) -> list[dict]:
    """Подменяет имена в полях name_fields на отображаемые ники (поиск по current_name)."""
    names = {row[f] for row in rows for f in name_fields if row.get(f)}
    if not names:
        return rows
    nicknames = await repo.nicknames(list(names))
    for row in rows:
        for f in name_fields:
            if row.get(f) in nicknames:
                row[f] = nicknames[row[f]]
    return rows


# ============ Чистые расчёты ============
def _bayes(games: int, rate: float, k: int, mean: float = 0.5) -> float:
    """Сглаживание доли к среднему: чем меньше игр, тем ближе к mean."""
    return games / (games + k) * rate + k / (games + k) * mean


def pair_stats(rows: list[dict]) -> list[dict]:
    """Пары игроков одной команды в матче: совместные игры и победы (не меньше MIN_PAIR_GAMES)."""
    by_match = defaultdict(list)
    for row in rows:
        by_match[row['match_uuid']].append(row)

    pairs = defaultdict(lambda: {'games': 0, 'wins': 0, 'names': {}})
    for players in by_match.values():
        if len(players) < 2:
            continue
        won = players[0]['won']
        for p1, p2 in combinations(players, 2):
            stats = pairs[tuple(sorted([p1['steam_id'], p2['steam_id']]))]
            stats['games'] += 1
            if won:
                stats['wins'] += 1
            stats['names'][p1['steam_id']] = p1.get('name', p1['steam_id'])
            stats['names'][p2['steam_id']] = p2.get('name', p2['steam_id'])

    return [
        {
            'name1': s['names'].get(a, a),
            'name2': s['names'].get(b, b),
            'games': s['games'],
            'wins': s['wins'],
            'losses': s['games'] - s['wins'],
            'win_rate': s['wins'] / s['games'],
        }
        for (a, b), s in pairs.items()
        if s['games'] >= MIN_PAIR_GAMES
    ]


def top_pairs(pairs: list[dict]) -> list[dict]:
    """Лучшие дуэты: по сглаженному винрейту."""
    ranked = sorted(pairs, key=lambda x: -_bayes(x['games'], x['win_rate'], MIN_PAIR_GAMES))
    return [dict(p) for p in ranked[:TOP_PAIRS_COUNT]]  # копии: ники подменяются на месте


def anti_pairs(pairs: list[dict]) -> list[dict]:
    """Антидуэты: по сглаженной доле поражений."""
    ranked = sorted(pairs, key=lambda x: _bayes(x['games'], 1 - x['win_rate'], MIN_PAIR_GAMES), reverse=True)
    return [dict(p) for p in ranked[:TOP_PAIRS_COUNT]]


def team_top_wr(totals: list[dict]) -> list[dict]:
    """Топ-10 по сглаженному винрейту из [{name, games, wins}]."""
    result = [
        {
            'name': r['name'] or 'Unknown',
            'games': r['games'],
            'wins': r['wins'],
            'losses': r['games'] - r['wins'],
            'win_rate': r['wins'] / r['games'],
        }
        for r in totals
    ]
    result.sort(key=lambda x: -_bayes(x['games'], x['win_rate'], MIN_TRAITORS_GAMES))
    return result[:10]


def current_streaks(rows: list[dict], streak_type: str) -> list[dict]:
    """Текущие серии побед (streak_type='win') или поражений: rows новые первыми. Топ-5."""
    by_player = defaultdict(list)
    for row in rows:
        by_player[row['steam_id']].append(row)

    result = []
    for steam_id, matches in by_player.items():
        streak = 0
        for match in matches:
            if bool(match.get('won')) != (streak_type == 'win'):
                break
            streak += 1
        if streak > 0:
            result.append({'name': matches[0].get('name', steam_id), 'streak': streak})
    result.sort(key=lambda x: -x['streak'])
    return result[:5]


def eternal_civilians(players: list[dict]) -> list[dict]:
    """Топ-5 с наименьшей сглаженной долей игр предателем."""
    result = []
    for p in players:
        total = p.get('ranked_games', 0)
        t_games = p.get('traitors_games', 0)
        if total == 0:
            continue
        result.append(
            {
                'name': get_display_name(p),
                'traitors_games': t_games,
                'total_games': total,
                'traitors_rate': t_games / total,
                'bayes_rate': _bayes(total, t_games / total, CIVILIAN_BAYES_K, TRAITORS_AVG_RATE),
            }
        )
    result.sort(key=lambda x: x['bayes_rate'])
    return result[:5]


def win_rate_stats(outcomes: dict[str, int]) -> dict:
    crew = outcomes.get('Crew Won', 0)
    traitors = outcomes.get('Traitors Won', 0)
    total = crew + traitors
    return {
        'crew_wins': crew,
        'traitors_wins': traitors,
        'total': total,
        'crew_wr': round(crew / total * 100, 1) if total else 0,
        'traitors_wr': round(traitors / total * 100, 1) if total else 0,
    }


def format_last_played(last_match_date: datetime | None, now: datetime) -> str:
    """Сколько прошло с последней игры: 5м, 3ч, 2дн, 4мес, 1г."""
    if not last_match_date:
        return ''
    delta = now - last_match_date
    seconds = int(delta.total_seconds())
    if seconds < 3600:
        return f'{seconds // 60}м' if seconds // 60 > 0 else 'сейчас'
    if seconds < 86400:
        return f'{seconds // 3600}ч'
    if delta.days < 30:
        return f'{delta.days}дн'
    if delta.days < 365:
        return f'{delta.days // 30}мес'
    return f'{delta.days // 365}г'


def build_player_line(i: int, player: dict, now: datetime) -> str:
    """Строка игрока в таблице."""
    is_calibrated = player.get('is_calibrated', False)
    place = medal(i, f'`#{i}`') if is_calibrated else f'`#{i}`'
    name = get_display_name(player)
    elo = player.get('elo', 1500)
    wins = player.get('wins', 0)
    losses = player.get('losses', 0)
    wr = round(player.get('win_rate', 0) * 100)
    penalty = ' ⚠️' if player.get('is_penalized') else ''

    s_games, s_wins = player.get('crew_games', 0), player.get('crew_wins', 0)
    t_games, t_wins = player.get('traitors_games', 0), player.get('traitors_wins', 0)
    team_stats = f" | 👥 {s_wins}W/{s_games - s_wins}L 👹 {t_wins}W/{t_games - t_wins}L"

    last_played = format_last_played(player.get('last_match_date'), now)
    last_played_str = f' `{last_played}`' if last_played else ''

    calibration = '' if is_calibrated else f" *({player.get('calibration_games', 0)}/{CALIBRATION_GAMES})*"
    return (
        f"{place} **{name}**{penalty} — **{elo}** ELO{calibration} | {wins}W/{losses}L {wr}%"
        f"{team_stats}{last_played_str}"
    )


def format_match_line(rec: dict, others: list[dict]) -> str:
    """Строка матча в истории игрока. others: остальные игроки матча [{name, team}] (с отображаемыми никами)."""
    result_icon = '✅' if rec.get('won') else '❌'
    change = rec.get('elo_change', 0)
    sign = '+' if change >= 0 else ''
    elo_after = rec.get('elo_after', 0)
    team = rec.get('team', '')
    team_icon = '👹' if team == 'Traitors' else '👥'
    dt = rec.get('match_datetime')
    date_str = dt.strftime('%d.%m %H:%M') if dt else '—'

    teammates = [p['name'] for p in others if p.get('team') == team]
    opponents = [p['name'] for p in others if p.get('team') != team]

    if team == 'Traitors':
        teammate_str = f' {teammates[0]}' if teammates else ''
        line = f'{result_icon} {sign}{change} ({elo_after}) · {team_icon}{teammate_str} · {date_str}'
        if opponents:
            line += f'\n{", ".join(opponents)}'
    else:
        enemy_str = f' vs 👹 {", ".join(opponents)}' if opponents else ''
        line = f'{result_icon} {sign}{change} ({elo_after}) · {team_icon}{enemy_str} · {date_str}'
        if teammates:
            line += f'\n{", ".join(teammates)}'
    return line


def clip(text: str, limit: int = 1024) -> str:
    """Обрезка под лимит поля embed."""
    return text[: limit - 3] + '...' if len(text) > limit else text


def total_pages(count: int, page_size: int) -> int:
    return max(1, (count + page_size - 1) // page_size)


# ============ Сбор данных ============
async def fetch_all_players(repo: LeaderboardRepo, season: int | None = None) -> list[dict]:
    if season is None:
        season = await season_now(repo)
    return await repo.ranked_players(season)


async def get_win_rate_stats(repo: LeaderboardRepo, season: int) -> dict:
    return win_rate_stats(await repo.outcome_counts(season))


async def match_lines(repo: LeaderboardRepo, steam_id: str, records: list[dict]) -> list[str]:
    lines = []
    for rec in records:
        others = []
        if rec.get('match_uuid'):
            others = [
                {'name': p.get('name', '?'), 'team': p.get('team')}
                for p in await repo.match_others(rec['match_uuid'], steam_id)
            ]
            await apply_changed_nicknames(repo, others, ['name'])
        lines.append(format_match_line(rec, others))
    return lines


async def past_season_placements(repo: LeaderboardRepo, steam_id: str, before_season: int) -> list[dict]:
    """Место и ELO игрока в прошлых сезонах (< before_season), новые первыми."""
    result = []
    for doc in await repo.past_seasons_of(steam_id, before_season):
        elo = doc.get('elo', 1500)
        result.append({'season': doc.get('season'), 'rank': await repo.rank(doc.get('season'), elo), 'elo': elo})
    return sorted(result, key=lambda x: x['season'], reverse=True)


async def past_seasons(repo: LeaderboardRepo) -> list[int]:
    """Прошлые сезоны (раньше текущего), в которых есть рейтинговые игроки, новые первыми."""
    current = await season_now(repo)
    return sorted((s for s in await repo.ranked_seasons() if isinstance(s, int) and s < current), reverse=True)


async def traitors_stats(repo: LeaderboardRepo, season: int) -> dict[str, list[dict]]:
    """Все блоки статистики Traitors для сообщения в канале и /season_stats (имена уже с отображаемыми никами)."""
    now = now_utc()
    traitors_pairs = pair_stats(await repo.team_rows(season, 'Traitors'))
    week = now - timedelta(days=7)
    wr_since = now - timedelta(days=TEAM_WR_DAYS)
    ranked = {'season': season, 'is_ranked': True, 'is_valid_match': True}
    traitors_week = await repo.team_rows_since(season, 'Traitors', week)
    crew_week = await repo.team_rows_since(season, 'Crew', week)

    async def recent_traitors(days: int) -> list[dict]:
        return await repo.top_by_rows(
            {**ranked, 'team': 'Traitors', 'match_datetime': {'$gte': now - timedelta(days=days)}}, 5
        )

    blocks = {
        'top_pairs': top_pairs(traitors_pairs),
        'anti_pairs': anti_pairs(traitors_pairs),
        'traitors_wr': team_top_wr(await repo.team_totals_since(season, 'Traitors', wr_since, MIN_TRAITORS_GAMES)),
        'crew_wr': team_top_wr(await repo.team_totals_since(season, 'Crew', wr_since, MIN_TRAITORS_GAMES)),
        'traitors_win_streaks': current_streaks(traitors_week, 'win'),
        'crew_win_streaks': current_streaks(crew_week, 'win'),
        'traitors_loss_streaks': current_streaks(traitors_week, 'loss'),
        'crew_loss_streaks': current_streaks(crew_week, 'loss'),
        'frequent_traitors': [
            {
                'name': get_display_name(p),
                'traitors_games': p.get('traitors_games', 0),
                'total_games': p.get('ranked_games', 0),
            }
            for p in await repo.most_traitors_games(season, 5)
        ],
        'double_dead': await repo.top_by_rows({**ranked, 'deaths': {'$gte': 2}}, 3),
        'recent_traitors_3d': await recent_traitors(3),
        'recent_traitors_7d': await recent_traitors(7),
        'eternal_civilians': eternal_civilians(await repo.players_with_games(season, CIVILIAN_MIN_GAMES)),
    }
    for key, rows in blocks.items():
        await apply_changed_nicknames(repo, rows, ['name1', 'name2'] if key.endswith('pairs') else ['name'])
    return blocks


# ============ График ============
async def generate_elo_chart(repo: LeaderboardRepo, steam_id: str, season: int) -> io.BytesIO | None:
    """PNG с ELO игрока за сезон (точки: победа/поражение/ручная правка). None, если точек меньше двух."""
    return await asyncio.to_thread(render_elo_chart, await repo.elo_points(steam_id, season))


def render_elo_chart(records: list[dict]) -> io.BytesIO | None:
    """Рисует график по записям elo_history (matplotlib, синхронно)."""
    if len(records) < 2:
        return None
    points = [r for r in records if r.get('match_datetime')]
    dates = [r['match_datetime'] for r in points]
    elos = [r['elo_after'] for r in points]
    if len(elos) < 2:
        return None

    fig, ax = plt.subplots(figsize=(8, 3), dpi=100)
    fig.patch.set_facecolor('#2b2d31')
    ax.set_facecolor('#2b2d31')
    ax.plot(dates, elos, color='#5865f2', linewidth=2, zorder=2)
    ax.fill_between(dates, elos, min(elos) - 20, alpha=0.15, color='#5865f2')

    for r in points:
        if r.get('manual'):
            ax.scatter(r['match_datetime'], r['elo_after'], color='white', s=35, marker='D', zorder=3)
        else:
            ax.scatter(
                r['match_datetime'], r['elo_after'], color='#57f287' if r.get('won') else '#ed4245', s=25, zorder=3
            )

    ax.annotate(
        f'{elos[-1]} ELO',
        xy=(dates[-1], elos[-1]),
        xytext=(-10, 10),
        textcoords='offset points',
        color='white',
        fontsize=9,
        fontweight='bold',
    )
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m'))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    plt.setp(ax.get_xticklabels(), color='#b5bac1', fontsize=8)
    plt.setp(ax.get_yticklabels(), color='#b5bac1', fontsize=8)
    for spine in ax.spines.values():
        spine.set_edgecolor('#3f4248')
    ax.tick_params(colors='#3f4248')
    ax.yaxis.grid(True, color='#3f4248', linestyle='--', linewidth=0.5)
    ax.set_axisbelow(True)
    fig.tight_layout(pad=0.5)

    buf = io.BytesIO()
    plt.savefig(buf, format='png', facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf
