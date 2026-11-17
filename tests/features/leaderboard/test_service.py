"""Расчёты лидерборда на маленьких примерах."""

from datetime import datetime, timedelta

from lobby_bot.features.leaderboard import service

NOW = datetime(2026, 10, 20, 12, 0)


def pair(name1, games, wins):
    return {
        'name1': name1,
        'name2': 'x',
        'games': games,
        'wins': wins,
        'losses': games - wins,
        'win_rate': wins / games,
    }


def test_pairs_sorted_by_smoothed_win_rate():
    # 3/3 = 100% на трёх играх сглаживается сильнее, чем 9/10 на десяти
    pairs = [pair('short', 3, 3), pair('long', 10, 9), pair('bad', 6, 1)]
    assert [p['name1'] for p in service.top_pairs(pairs)] == ['long', 'short', 'bad']
    assert [p['name1'] for p in service.anti_pairs(pairs)] == ['bad', 'short', 'long']


def test_pair_stats_counts_only_together_games():
    rows = [
        {'match_uuid': m, 'steam_id': s, 'name': s, 'won': w}
        for m, w in (('m1', True), ('m2', False), ('m3', True))
        for s in ('A', 'B')
    ]
    rows += [{'match_uuid': 'm4', 'steam_id': 'A', 'name': 'A', 'won': True}]  # A один: не пара
    [p] = service.pair_stats(rows)
    assert (p['games'], p['wins'], p['losses']) == (3, 2, 1)
    assert service.pair_stats(rows[:4]) == []  # меньше MIN_PAIR_GAMES совместных игр


def test_current_streaks():
    rows = [{'steam_id': 'A', 'name': 'A', 'won': w} for w in (True, True, False, True)]
    rows += [{'steam_id': 'B', 'name': 'B', 'won': w} for w in (False, False, True)]
    assert service.current_streaks(rows, 'win') == [{'name': 'A', 'streak': 2}]
    assert service.current_streaks(rows, 'loss') == [{'name': 'B', 'streak': 2}]


def test_eternal_civilians_smoothing():
    players = [
        {'current_name': 'never_7', 'ranked_games': 7, 'traitors_games': 0},
        {'current_name': 'rare_40', 'ranked_games': 40, 'traitors_games': 4},
        {'current_name': 'often', 'ranked_games': 20, 'traitors_games': 10},
    ]
    assert [p['name'] for p in service.eternal_civilians(players)] == ['rare_40', 'never_7', 'often']


def test_format_last_played():
    assert service.format_last_played(None, NOW) == ''
    assert service.format_last_played(NOW - timedelta(seconds=30), NOW) == 'сейчас'
    assert service.format_last_played(NOW - timedelta(minutes=5), NOW) == '5м'
    assert service.format_last_played(NOW - timedelta(hours=3), NOW) == '3ч'
    assert service.format_last_played(NOW - timedelta(days=40), NOW) == '1мес'
    assert service.format_last_played(NOW - timedelta(days=800), NOW) == '2г'


def test_win_rate_stats():
    assert service.win_rate_stats({'Crew Won': 3, 'Traitors Won': 1}) == {
        'crew_wins': 3,
        'traitors_wins': 1,
        'total': 4,
        'crew_wr': 75.0,
        'traitors_wr': 25.0,
    }
    assert service.win_rate_stats({})['crew_wr'] == 0
