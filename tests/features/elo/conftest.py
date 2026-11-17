"""Синтетические рейтинговые матчи: сезон, который задевает все правила расчёта ELO."""

import random
from datetime import datetime, timedelta

ROLES_CREW = ['Commander', 'Pilot', 'Mechanic', 'Medic', 'Priest', 'Guard', 'Quartermaster', 'Scout']
ROLES_TRAITOR = ['Traitor']


def make_match(
    uuid: str, players: list[tuple[str, str]], traitors_won: bool, when: datetime, role_seed: int = 0
) -> list[dict]:
    """Строки history_player_stats одного матча. players: [(steam_id, 'Crew'|'Traitors')]."""
    outcome = 'Traitors Win' if traitors_won else 'Crew Win'
    rows = []
    for n, (steam_id, team) in enumerate(players):
        roles = ROLES_TRAITOR if team == 'Traitors' else ROLES_CREW
        rows.append(
            {
                'match_uuid': uuid,
                'steam_id': steam_id,
                'game_uid': f'game-{steam_id}',
                'name': f'name-{steam_id}',
                'team': team,
                'role': roles[(n + role_seed) % len(roles)],
                'won': (team == 'Traitors') == traitors_won,
                'match_outcome': outcome,
                'match_datetime': when,
                'match_duration_seconds': 1800,
                'map': 'Outpost',
                'escape_progress': 0.5,
                'is_ranked': True,
                'is_valid_match': True,
            }
        )
    return rows


def synthetic_season(seed: int = 7, matches: int = 150):
    """Сезон со всем, что влияет на расчёт: калибровка, длинные стрики, штрафы, твинки.

    Возвращает (строки матчей, флаги игроков, порядок матчей).
    """
    rnd = random.Random(seed)
    pool = [f'P{i:02d}' for i in range(30)]
    flags = {
        'P01': {'win_multiplier': 0.7, 'loss_multiplier': 1.3},  # штраф
        'P02': {'win_multiplier': 1.2, 'loss_multiplier': 1.0},  # бонус
        'T01': {'is_twink': True, 'linked_to': 'P03'},  # твинк, основной играет
        'T02': {'is_twink': True, 'linked_to': 'MAIN_OFF'},  # основной не играет никогда
        'T03': {'is_twink': True, 'linked_to': 'NO_RATING'},  # у основного нет рейтинга
        'T04': {'is_twink': True, 'linked_to': 'P04'},  # второй твинк того же основного
        'T05': {'is_twink': True, 'linked_to': 'P04'},
        'T06': {'is_twink': True, 'linked_to': None},  # флаг без привязки
        'MAIN_OFF': {'win_multiplier': 0.5},
    }
    # MAIN_OFF есть в рейтинге, но не в матчах: создаём его рейтинг через флаги
    pool += ['T01', 'T02', 'T03', 'T04', 'T05', 'T06']
    start = datetime(2026, 9, 1)
    rows, order = [], []
    favourite = 'P05'  # часто побеждает за предателя: длинные винстрики
    for m in range(matches):
        players = rnd.sample(pool, 8)
        if m % 5 == 0 and favourite not in players:
            players[0] = favourite
        traitors = set(players[: rnd.choice([2, 2, 3])]) if rnd.random() < 0.5 else set(players[-2:])
        if favourite in players and m % 5 == 0:
            traitors = {favourite, players[1]}
        traitors_won = rnd.random() < (0.8 if favourite in traitors else 0.4)
        uuid = f'M{m:04d}'
        rows += make_match(
            uuid,
            [(p, 'Traitors' if p in traitors else 'Crew') for p in players],
            traitors_won,
            start + timedelta(hours=m),
            role_seed=m,
        )
        order.append(uuid)
    # Длинный винстрик за экипажа: P06 побеждает 8 раз подряд
    for k in range(8):
        players = ['P06', *rnd.sample([p for p in pool if p != 'P06'], 7)]
        uuid = f'S{k:02d}'
        rows += make_match(
            uuid,
            [(p, 'Traitors' if n >= 6 else 'Crew') for n, p in enumerate(players)],
            False,
            start + timedelta(hours=matches + k),
            role_seed=k,
        )
        order.append(uuid)
    # Длинный винстрик за предателя: P07 побеждает 7 раз подряд
    for k in range(7):
        players = ['P07', *rnd.sample([p for p in pool if p not in ('P06', 'P07')], 7)]
        uuid = f'T{k:02d}'
        rows += make_match(
            uuid,
            [(p, 'Traitors' if n < 2 else 'Crew') for n, p in enumerate(players)],
            True,
            start + timedelta(hours=matches + 10 + k),
            role_seed=k,
        )
        order.append(uuid)
    # Матчи, которые нельзя посчитать: одна команда и нерейтинговый
    rows += make_match('ONE_TEAM', [('P10', 'Crew'), ('P11', 'Crew')], False, start)
    unranked = make_match('UNRANKED', [('P10', 'Crew'), ('P11', 'Traitors')], True, start)
    for r in unranked:
        r['is_ranked'] = False
    rows += unranked
    order += ['ONE_TEAM', 'UNRANKED', 'MISSING']
    return rows, flags, order
