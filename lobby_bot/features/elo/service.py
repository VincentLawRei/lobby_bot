"""ELO: расчёт рейтинга (калибровка, винстрики, штрафы, твинки), очередь матчей, decay, ручные бонусы.

Расчёт матча (`calculate_match`) и среза за инактив (`decay_rating`) — чистые функции без базы:
на вход рейтинги до матча, на выход что записать. Работа с базой в `process_match_elo` и др.: асинхронные
функции, первым аргументом EloRepo. Поведение зафиксировано эталонами в tests/features/elo/snapshots.
"""

import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from .repo import EloRepo

logger = logging.getLogger(__name__)

# Константы ELO системы
STARTING_ELO = 1500
CALIBRATION_GAMES = 10
K_BASE = 32
K_CALIBRATION = 50

# Множители для Traitors
TRAITORS_WIN_MULTIPLIER = 1.6  # Больше получают за победу
TRAITORS_LOSS_MULTIPLIER = 0.6  # Меньше теряют за поражение

# Множители для начисления ELO основному аккаунту за твинка
TWINK_WIN_MULTIPLIER = 1
TWINK_LOSS_MULTIPLIER = 1
# Основной аккаунт, чей твинк в этом же матче: потери при поражении x2
MAIN_WITH_TWINK_LOSS_MULTIPLIER = 2

# Винстрик бонусы (победы подряд за одну команду): (с какого стрика, множитель)
WINSTREAK_THRESHOLDS_TRAITOR = [(2, 1.05), (4, 1.15), (6, 1.25)]
WINSTREAK_THRESHOLDS_CREW = [(3, 1.05), (5, 1.10), (7, 1.20)]

# Decay (срез за инактив)
DECAY_THRESHOLD_DAYS = 7  # окно инактива
DECAY_ELO_PER_DAY = 50  # сколько снимать за один раз
DECAY_FLOOR_ELO = 1550  # ниже этого порога не снимаем
DECAY_COOLDOWN = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)  # в Mongo даты наивные в UTC


# ============ Чистые функции расчёта ============
def new_rating(steam_id: str, game_uid, name: str, season: int, now: datetime) -> dict:
    """Рейтинг нового игрока в сезоне."""
    return {
        'steam_id': steam_id,
        'game_uid': game_uid,
        'current_name': name,
        'season': season,
        'elo': STARTING_ELO,
        'peak_elo': STARTING_ELO,
        'total_games': 0,
        'ranked_games': 0,
        'wins': 0,
        'losses': 0,
        'win_rate': 0.0,
        'crew_games': 0,
        'crew_wins': 0,
        'traitors_games': 0,
        'traitors_wins': 0,
        'role_stats': {},
        'is_calibrated': False,
        'calibration_games': 0,
        'crew_winstreak': 0,
        'traitor_winstreak': 0,
        'crew_losestreak': 0,
        'traitor_losestreak': 0,
        'last_match_date': None,
        'created_at': now,
        'updated_at': now,
    }


def calculate_expected_score(elo_a: float, elo_b: float) -> float:
    """Вероятность победы A: E_A = 1 / (1 + 10^((R_B - R_A) / 400))."""
    return 1 / (1 + math.pow(10, (elo_b - elo_a) / 400))


def get_winstreak_multiplier(streak: int, is_traitor: bool) -> float:
    thresholds = WINSTREAK_THRESHOLDS_TRAITOR if is_traitor else WINSTREAK_THRESHOLDS_CREW
    for threshold, bonus in reversed(thresholds):
        if streak >= threshold:
            return bonus
    return 1.0


def calculate_elo_change(
    player_elo, team_avg_elo, opponent_avg_elo, won, is_calibration, is_traitor_player, winstreak=0
) -> int:
    k = K_CALIBRATION if is_calibration else K_BASE
    # Ожидание: 70% командное, 30% индивидуальное
    expected_team = calculate_expected_score(team_avg_elo, opponent_avg_elo)
    expected_individual = calculate_expected_score(player_elo, opponent_avg_elo)
    expected = expected_team * 0.7 + expected_individual * 0.3

    elo_change = k * ((1.0 if won else 0.0) - expected)

    if is_traitor_player:
        elo_change *= TRAITORS_WIN_MULTIPLIER if won else TRAITORS_LOSS_MULTIPLIER

    # Винстрик бонус только при победе
    if won:
        elo_change *= get_winstreak_multiplier(winstreak, is_traitor_player)

    return round(elo_change)


@dataclass
class PlayerResult:
    """Что записать по игроку матча."""

    steam_id: str
    match_fields: dict  # $set в history_player_stats
    rating_fields: dict | None = None  # $set в player_ratings (None: рейтинг не меняется)
    history: dict | None = None  # запись в elo_history


@dataclass
class MainResult:
    """ELO основному аккаунту за игру его твинка (основного в матче не было)."""

    steam_id: str
    elo_before: int
    elo_after: int
    rating_fields: dict
    history: dict


@dataclass
class MatchCalculation:
    match: dict  # $set в matches
    players: list[PlayerResult] = field(default_factory=list)
    mains: list[MainResult] = field(default_factory=list)


def _rating_update(
    rating: dict, player: dict, won: bool, is_traitor: bool, is_calibration: bool, new_elo: int, now: datetime
) -> dict:
    """Поля рейтинга после матча: счётчики, стрики, роли, калибровка."""
    ranked_games = rating.get('ranked_games', 0) + 1
    wins = rating.get('wins', 0) + (1 if won else 0)
    update = {
        'elo': new_elo,
        'peak_elo': max(rating.get('peak_elo', new_elo), new_elo),
        'total_games': rating.get('total_games', 0) + 1,
        'ranked_games': ranked_games,
        'wins': wins,
        'losses': rating.get('losses', 0) + (0 if won else 1),
        'win_rate': round(wins / ranked_games, 3),
        'current_name': player['name'],
        'last_match_date': player.get('match_datetime'),
        'updated_at': now,
    }

    team, games, wins_key = (
        ('traitor', 'traitors_games', 'traitors_wins') if is_traitor else ('crew', 'crew_games', 'crew_wins')
    )
    update[games] = rating.get(games, 0) + 1
    if won:
        update[wins_key] = rating.get(wins_key, 0) + 1
        update[f'{team}_winstreak'] = rating.get(f'{team}_winstreak', 0) + 1
        update[f'{team}_losestreak'] = 0
    else:
        update[f'{team}_winstreak'] = 0
        update[f'{team}_losestreak'] = rating.get(f'{team}_losestreak', 0) + 1

    role_stats = {role: dict(stats) for role, stats in rating.get('role_stats', {}).items()}
    role = role_stats.setdefault(player['role'], {'games': 0, 'wins': 0})
    role['games'] += 1
    if won:
        role['wins'] += 1
    update['role_stats'] = role_stats

    if is_calibration:
        update['calibration_games'] = rating.get('calibration_games', 0) + 1
        if update['calibration_games'] >= CALIBRATION_GAMES:
            update['is_calibrated'] = True
    return update


def calculate_match(
    match_uuid: str,
    players: list[dict],
    ratings: dict[str, dict],
    main_ratings: dict[str, dict],
    season: int,
    now: datetime,
) -> MatchCalculation | None:
    """Расчёт ELO матча.

    players: строки history_player_stats матча; ratings: рейтинги игроков матча до матча (steam_id -> doc);
    main_ratings: рейтинги основных аккаунтов твинков, которых нет в матче.
    None, если матч нельзя посчитать (нет игроков или одной из команд).
    """
    crew = [p for p in players if p['team'] == 'Crew']
    traitors = [p for p in players if p['team'] == 'Traitors']
    if not crew or not traitors:
        return None

    first = players[0]
    outcome = first.get('match_outcome', '')

    # ---- Твинки ----
    # Основной в матче: твинк не получает ELO, основной теряет x2 при поражении.
    # Основного нет: ELO твинка уходит основному, в среднем команды твинк считается с ELO основного.
    in_match = {p['steam_id'] for p in players}
    twink_map: dict[str, str] = {}  # твинк -> основной (основного нет в матче)
    twink_blocked: set[str] = set()  # твинки, чей основной в матче
    main_with_twink: set[str] = set()
    for p in crew + traitors:
        rating = ratings[p['steam_id']]
        linked_to = rating.get('linked_to') if rating.get('is_twink') else None
        if not linked_to:
            continue
        if linked_to in in_match:
            twink_blocked.add(rating['steam_id'])
            main_with_twink.add(linked_to)
        else:
            twink_map[rating['steam_id']] = linked_to

    def effective_elo(steam_id: str) -> int:
        main = main_ratings.get(twink_map.get(steam_id))
        return main['elo'] if main else ratings[steam_id]['elo']

    avg_crew = sum(effective_elo(p['steam_id']) for p in crew) / len(crew)
    avg_traitors = sum(effective_elo(p['steam_id']) for p in traitors) / len(traitors)

    result = MatchCalculation(
        match={
            'match_uuid': match_uuid,
            'match_datetime': first.get('match_datetime'),
            'duration_seconds': first.get('match_duration_seconds', 0),
            'outcome': outcome,
            'map': first.get('map'),
            'escape_progress': first.get('escape_progress', 0),
            'players_count': len(players),
            'is_ranked': True,
            'is_valid_match': True,
            'season': season,
            'avg_elo_crew': round(avg_crew),  # средние ДО матча
            'avg_elo_traitors': round(avg_traitors),
            'crew_count': len(crew),
            'traitors_count': len(traitors),
            'processed_at': now,
        }
    )

    main_changes: dict[str, int] = {}  # основной -> сумма изменений от его твинков
    for player in players:
        sid = player['steam_id']
        rating = ratings[sid]
        won = player['won']
        is_traitor = player['team'] == 'Traitors'
        team_avg, opponent_avg = (avg_traitors, avg_crew) if is_traitor else (avg_crew, avg_traitors)
        unchanged = {'elo_before': rating['elo'], 'elo_after': rating['elo'], 'elo_change': 0, 'season': season}

        if sid in twink_blocked:
            result.players.append(PlayerResult(sid, unchanged))
            continue

        if sid in twink_map:
            main_sid = twink_map[sid]
            main = main_ratings.get(main_sid)
            if main:
                change = calculate_elo_change(
                    main['elo'],
                    team_avg,
                    opponent_avg,
                    won,
                    main.get('calibration_games', 0) < CALIBRATION_GAMES,
                    is_traitor,
                    main.get('traitor_winstreak' if is_traitor else 'crew_winstreak', 0),
                )
                change = round(change * (TWINK_WIN_MULTIPLIER if won else TWINK_LOSS_MULTIPLIER))
                main_changes[main_sid] = main_changes.get(main_sid, 0) + change
            result.players.append(PlayerResult(sid, {**unchanged, 'twink_redirected_to': main_sid}))
            continue

        is_calibration = rating.get('calibration_games', 0) < CALIBRATION_GAMES
        change = calculate_elo_change(
            rating['elo'],
            team_avg,
            opponent_avg,
            won,
            is_calibration,
            is_traitor,
            rating.get('traitor_winstreak' if is_traitor else 'crew_winstreak', 0),
        )

        # Ручной штраф/бонус игрока
        multiplier = rating.get('win_multiplier', 1.0) if won else rating.get('loss_multiplier', 1.0)
        if sid in main_with_twink and not won:
            multiplier *= MAIN_WITH_TWINK_LOSS_MULTIPLIER
        if multiplier != 1.0:
            change = round(change * multiplier)

        old_elo = rating['elo']
        new_elo = old_elo + change
        result.players.append(
            PlayerResult(
                sid,
                match_fields={'elo_before': old_elo, 'elo_after': new_elo, 'elo_change': change, 'season': season},
                rating_fields=_rating_update(rating, player, won, is_traitor, is_calibration, new_elo, now),
                history={
                    'steam_id': sid,
                    'game_uid': player['game_uid'],
                    'match_uuid': match_uuid,
                    'season': season,
                    'elo_before': old_elo,
                    'elo_after': new_elo,
                    'elo_change': change,
                    'team': player['team'],
                    'role': player['role'],
                    'won': won,
                    'match_datetime': player.get('match_datetime'),
                    'created_at': now,
                },
            )
        )

    for main_sid, change in main_changes.items():
        if main_sid in twink_blocked:
            continue
        main = main_ratings[main_sid]
        old_elo = main['elo']
        new_elo = old_elo + change
        result.mains.append(
            MainResult(
                main_sid,
                old_elo,
                new_elo,
                rating_fields={
                    'elo': new_elo,
                    'peak_elo': max(main.get('peak_elo', new_elo), new_elo),
                    'updated_at': now,
                },
                history={
                    'steam_id': main_sid,
                    'match_uuid': match_uuid,
                    'season': season,
                    'elo_before': old_elo,
                    'elo_after': new_elo,
                    'elo_change': change,
                    'twink_penalty': True,
                    'match_datetime': first.get('match_datetime'),
                    'created_at': now,
                },
            )
        )
    return result


def decay_rating(player: dict, now: datetime) -> tuple[dict, dict, dict]:
    """Срез за инактив: ($set рейтинга, запись elo_history, данные для поста)."""
    old_elo = player['elo']
    new_elo = old_elo - DECAY_ELO_PER_DAY
    steam_id = player['steam_id']
    fields = {'elo': new_elo, 'last_decay_date': now, 'updated_at': now}
    history = {
        'steam_id': steam_id,
        'match_uuid': 'DECAY_INACTIVITY',
        'season': player['season'],
        'elo_before': old_elo,
        'elo_after': new_elo,
        'elo_change': -DECAY_ELO_PER_DAY,
        'manual': True,
        'won': False,
        'match_datetime': now,
        'created_at': now,
    }
    info = {'name': player.get('current_name', f"SteamID: {steam_id}"), 'old_elo': old_elo, 'new_elo': new_elo}
    return fields, history, info


# ============ Работа с базой ============
async def get_or_create_player_rating(repo: EloRepo, player: dict, season: int, now: datetime) -> dict:
    """Рейтинг игрока в сезоне; создаётся, если нет. game_uid и имя обновляются (могли измениться)."""
    steam_id = player['steam_id']
    rating = await repo.rating(steam_id, season)
    if not rating:
        rating = new_rating(steam_id, player['game_uid'], player['name'], season, now)
        await repo.insert_rating(rating)
        return rating
    fields = {'game_uid': player['game_uid'], 'current_name': player['name']}
    await repo.update_rating(steam_id, season, fields)
    rating.update(fields)
    return rating


async def plan_match_elo(
    repo: EloRepo, match_uuid: str, season: int, now: datetime | None = None
) -> MatchCalculation | None:
    """Посчитать ELO матча по текущим рейтингам, ничего не меняя в рейтингах (создаёт только рейтинги новых
    игроков). None, если матч нельзя посчитать."""
    now = now or _now()
    players = await repo.ranked_match_players(match_uuid)
    crew = [p for p in players if p['team'] == 'Crew']
    traitors = [p for p in players if p['team'] == 'Traitors']
    if not crew or not traitors:
        return None

    ratings = {p['steam_id']: await get_or_create_player_rating(repo, p, season, now) for p in crew + traitors}
    main_ids = {
        r['linked_to']
        for r in ratings.values()
        if r.get('is_twink') and r.get('linked_to') and r['linked_to'] not in ratings
    }
    main_ratings = {sid: doc for sid in main_ids if (doc := await repo.rating(sid, season))}
    return calculate_match(match_uuid, players, ratings, main_ratings, season, now)


async def apply_match_elo(repo: EloRepo, match_uuid: str, season: int, calc: MatchCalculation) -> None:
    """Записать расчёт матча. Повторный вызов с тем же расчётом даёт тот же результат: рейтинги и строки
    матча ставятся абсолютными значениями ($set), запись elo_history одна на игрока матча."""
    await repo.save_match(match_uuid, calc.match)
    for pr in calc.players:
        if pr.rating_fields is not None:
            await repo.update_rating(pr.steam_id, season, pr.rating_fields)
            await repo.add_match_elo_history(pr.history)
        await repo.set_player_match_elo(match_uuid, pr.steam_id, pr.match_fields)
    for main in calc.mains:
        await repo.update_rating(main.steam_id, season, main.rating_fields)
        await repo.add_match_elo_history(main.history)
        logger.info(
            "[TWINK] Основной %s: %s → %s (%+d)",
            main.steam_id,
            main.elo_before,
            main.elo_after,
            main.elo_after - main.elo_before,
        )


async def process_match_elo(repo: EloRepo, match_uuid: str, season: int, now: datetime | None = None) -> bool:
    """Посчитать и записать ELO матча. False, если матч нельзя посчитать."""
    calc = await plan_match_elo(repo, match_uuid, season, now)
    if calc is None:
        return False
    await apply_match_elo(repo, match_uuid, season, calc)
    return True


def _plan_to_doc(calc: MatchCalculation) -> dict:
    return asdict(calc)


def _plan_from_doc(doc: dict) -> MatchCalculation:
    return MatchCalculation(
        match=doc['match'],
        players=[PlayerResult(**p) for p in doc['players']],
        mains=[MainResult(**m) for m in doc['mains']],
    )


async def process_queued_match(repo: EloRepo, match_uuid: str, season: int) -> bool:
    """Посчитать матч из очереди и отметить обработанным. Непосчитанный остаётся в очереди.

    Расчёт сначала сохраняется в документ очереди и только потом пишется в рейтинги. Если процесс упал
    посреди записи, при следующем проходе дописывается сохранённый расчёт, а не новый: новый взял бы уже
    изменённые рейтинги и начислил ELO второй раз.
    """
    saved = await repo.queued_plan(match_uuid)
    if saved is not None:
        logger.warning("[ELO] Матч %s: дописываю расчёт, прерванный прошлым проходом", match_uuid)
        calc = _plan_from_doc(saved)
    else:
        calc = await plan_match_elo(repo, match_uuid, season)
        if calc is None:
            return False
        await repo.save_plan(match_uuid, _plan_to_doc(calc))
    await apply_match_elo(repo, match_uuid, season, calc)
    await repo.mark_processed(match_uuid, season, _now())
    return True


async def process_elo_decay(repo: EloRepo, season: int, now: datetime | None = None) -> list[dict]:
    """Срезать ELO неактивным игрокам. Возвращает [{name, old_elo, new_elo}] для поста."""
    now = now or _now()
    decayed = []
    for player in await repo.decay_candidates(
        season, DECAY_FLOOR_ELO, now - timedelta(days=DECAY_THRESHOLD_DAYS), now - DECAY_COOLDOWN
    ):
        fields, history, info = decay_rating(player, now)
        await repo.update_rating(player['steam_id'], season, fields)
        await repo.add_elo_history(history)
        decayed.append(info)
        logger.info("[DECAY] Игрок %s оштрафован: %s -> %s", info['name'], info['old_elo'], info['new_elo'])
    return decayed


# ============ Для модераторских команд и других модулей ============
async def set_player_penalty(
    repo: EloRepo, steam_id: str, win_multiplier: float, loss_multiplier: float, reason: str | None, season: int
) -> None:
    """Штраф/бонус игрока: множители ELO за победу и поражение."""
    is_penalized = win_multiplier != 1.0 or loss_multiplier != 1.0
    await repo.update_rating(
        steam_id,
        season,
        {
            'win_multiplier': win_multiplier,
            'loss_multiplier': loss_multiplier,
            'is_penalized': is_penalized,
            'penalty_reason': reason,
            'penalized_at': _now() if is_penalized else None,
        },
    )
    logger.info("Штраф установлен для %s: win=%sx, loss=%sx", steam_id, win_multiplier, loss_multiplier)


async def link_twink(repo: EloRepo, twink_steam_id: str, main_steam_id: str, season: int) -> None:
    """Привязать твинк к основному аккаунту: дальше ELO твинка уходит основному."""
    await repo.update_rating(
        twink_steam_id, season, {'is_twink': True, 'linked_to': main_steam_id, 'linked_at': _now()}
    )
    logger.info("[TWINK] %s привязан к %s", twink_steam_id, main_steam_id)


async def unlink_twink(repo: EloRepo, twink_steam_id: str, season: int) -> None:
    await repo.update_rating(twink_steam_id, season, {'is_twink': False, 'linked_to': None, 'linked_at': None})
    logger.info("[TWINK] Привязка снята с %s", twink_steam_id)


async def elo_overrides(repo: EloRepo) -> dict[str, int]:
    """Ручные бонусы веса на предателя: steam_id -> бонус (коллекция elo_overrides, правится в MongoDB)."""
    return await repo.overrides()


# ============ Модераторские команды ============
@dataclass
class EloChange:
    steam_id: str
    name: str
    elo_before: int
    elo_after: int
    change: int


async def change_elo(
    repo: EloRepo, steam_id: str, points: int, season: int, now: datetime, history: dict
) -> EloChange | None:
    """Изменить ELO игрока сезона на points и записать в историю (поля history + стандартные). None — нет игрока."""
    player = await repo.rating(steam_id, season)
    if not player:
        return None
    old_elo = player.get('elo', 1500)
    new_elo = old_elo + points
    await repo.update_rating(
        steam_id, season, {'elo': new_elo, 'peak_elo': max(new_elo, player.get('peak_elo', new_elo)), 'updated_at': now}
    )
    await repo.add_elo_history(
        {
            'steam_id': steam_id,
            'season': season,
            'elo_before': old_elo,
            'elo_after': new_elo,
            'elo_change': points,
            **history,
            'match_datetime': now,
            'manual': True,
        }
    )
    return EloChange(steam_id, player.get('current_name', steam_id), old_elo, new_elo, points)


async def adjust_elo(
    repo: EloRepo,
    steam_id: str,
    points: int,
    reason: str,
    by_id: str,
    by_name: str,
    season: int,
    now: datetime | None = None,
) -> EloChange | None:
    """Ручная правка ELO модератором."""
    return await change_elo(
        repo,
        steam_id,
        points,
        season,
        now or _now(),
        {'reason': reason, 'adjusted_by': by_id, 'adjusted_by_name': by_name},
    )


@dataclass
class RevertResult:
    match_uuid: str
    is_undo: bool  # True: отмена прошлого отката (изменения применены снова)
    lines: list[str]


async def revert_match(
    repo: EloRepo, uuid_prefix: str, season: int, by_id: str, by_name: str, now: datetime | None = None
) -> RevertResult | None:
    """Откат ELO матча; повторный вызов отменяет откат (чётность числа откатов по первому игроку).

    Ищет по началу UUID; None, если матч не найден в истории ELO.
    """
    now = now or _now()
    entries = await repo.match_entries(uuid_prefix, season)
    if not entries:
        return None
    full_uuid = entries[0]['match_uuid']
    is_undo = await repo.revert_count(full_uuid, entries[0]['steam_id'], season) % 2 == 1

    lines = []
    for entry in entries:
        apply_change = entry['elo_change'] if is_undo else -entry['elo_change']
        result = await change_elo(
            repo,
            entry['steam_id'],
            apply_change,
            season,
            now,
            {
                'reverted_match_uuid': full_uuid,
                'is_undo_revert': is_undo,
                'reason': f"{'Отмена отката' if is_undo else 'Откат'} матча {full_uuid[:8]}",
                'adjusted_by': by_id,
                'adjusted_by_name': by_name,
            },
        )
        if not result:
            lines.append(f"❌ `{entry['steam_id']}` — не найден")
            continue
        sign = "+" if apply_change > 0 else ""
        lines.append(f"**{result.name}**: {result.elo_before} → **{result.elo_after}** ({sign}{apply_change})")
    return RevertResult(full_uuid, is_undo, lines)


async def recent_matches_for_autocomplete(
    repo: EloRepo, season: int, uuid_prefix: str
) -> list[tuple[str, datetime | None]]:
    """До 25 последних матчей сезона: [(match_uuid, дата)]."""
    seen: dict[str, datetime | None] = {}
    for entry in await repo.recent_match_entries(season, uuid_prefix):
        uuid = entry.get('match_uuid')
        if not uuid or uuid in seen:
            continue
        seen[uuid] = entry.get('match_datetime')
        if len(seen) >= 25:
            break
    return list(seen.items())


async def set_penalty(
    repo: EloRepo,
    steam_id: str,
    win_multiplier: float,
    loss_multiplier: float,
    reason: str,
    until: datetime | None,
    by_id: str,
    by_name: str,
    season: int,
) -> None:
    """Штраф со сроком (until=None — бессрочно) и автором."""
    await set_player_penalty(repo, steam_id, win_multiplier, loss_multiplier, reason, season)
    await repo.update_rating(
        steam_id, season, {'penalty_until': until, 'penalty_set_by': by_id, 'penalty_set_by_name': by_name}
    )


async def remove_penalty(repo: EloRepo, steam_id: str, season: int) -> None:
    await set_player_penalty(repo, steam_id, 1.0, 1.0, None, season)
    await repo.update_rating(steam_id, season, {'penalty_until': None})


async def expire_penalties(repo: EloRepo, season: int, now: datetime | None = None) -> list[dict]:
    """Снять штрафы с истёкшим сроком. Возвращает снятых игроков (документы рейтинга)."""
    expired = await repo.expired_penalties(season, now or _now())
    for player in expired:
        await remove_penalty(repo, player['steam_id'], season)
        logger.info(
            "[PENALTY] Штраф снят автоматически: %s (%s)",
            player.get('current_name', player['steam_id']),
            player['steam_id'],
        )
    return expired
