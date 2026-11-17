"""Расчёт ELO на синтетическом сезоне и срез за инактив сверяются с эталонами в snapshots/ (всё, что расчёт
пишет в базу)."""

from datetime import datetime, timedelta

from lobby_bot.features.elo import service
from tests.snapshot import assert_snapshot

from . import replay
from .conftest import synthetic_season

SEASON = 3


async def test_synthetic_season(request):
    rows, flags, order = synthetic_season()
    database = replay.new_database()
    replay.seed(database, rows, flags, SEASON)
    results = await replay.run_new(database, order, SEASON)
    assert_snapshot(request, {'processed': results, **replay.plain(replay.snapshot(database))})


async def test_synthetic_season_covers_all_rules():
    """Данные действительно задевают все ветки расчёта, иначе эталон ничего не доказывает."""
    rows, flags, order = synthetic_season()
    database = replay.new_database()
    replay.seed(database, rows, flags, SEASON)
    results = await replay.run_new(database, order, SEASON)

    assert results[-3:] == [False, False, False]  # одна команда, нерейтинговый, нет строк
    stats = list(database.history_player_stats.find({'season': SEASON}))
    assert any(s.get('twink_redirected_to') == 'MAIN_OFF' for s in stats)  # ELO твинка ушло основному
    assert any(s.get('twink_redirected_to') == 'NO_RATING' for s in stats)  # основного нет в рейтинге
    blocked = [s for s in stats if s['steam_id'] == 'T01' and 'twink_redirected_to' not in s]
    assert blocked and all(s['elo_change'] == 0 for s in blocked)  # твинк в матче с основным
    assert database.elo_history.count_documents({'twink_penalty': True, 'steam_id': 'P04'}) > 0
    ratings = {r['steam_id']: r for r in database.player_ratings.find()}
    assert ratings['P05']['is_calibrated']
    assert ratings['P07']['traitor_winstreak'] >= 7
    assert ratings['P06']['crew_winstreak'] >= 8
    history = list(database.elo_history.find({'steam_id': 'P05'}))
    assert any(h['won'] and h['team'] == 'Traitors' for h in history)


def decay_players(now):
    return [
        {'steam_id': 'A', 'elo': 1700, 'last_match_date': now - timedelta(days=10)},  # срез
        {
            'steam_id': 'B',
            'elo': 1560,
            'last_match_date': now - timedelta(days=30),
            'last_decay_date': now - timedelta(hours=30),
            'current_name': 'Bob',
        },  # срез
        {'steam_id': 'C', 'elo': 1550, 'last_match_date': now - timedelta(days=30)},  # порог
        {'steam_id': 'D', 'elo': 1800, 'last_match_date': now - timedelta(days=2)},  # активен
        {
            'steam_id': 'E',
            'elo': 1800,
            'last_match_date': now - timedelta(days=30),
            'last_decay_date': now - timedelta(hours=2),
        },  # уже срезан
        {'steam_id': 'F', 'elo': 1800, 'last_match_date': now - timedelta(days=30), 'season': 1},  # другой сезон
    ]


async def test_decay(request):
    now = datetime(2026, 10, 20, 12, 0)
    database = replay.new_database()
    database.player_ratings.insert_many([{'season': SEASON, **p} for p in decay_players(now)])
    info = await service.process_elo_decay(replay.make_repo(database), SEASON, now)

    assert [p['name'] for p in info] == ['SteamID: A', 'Bob']
    plain = lambda docs: [{k: v for k, v in d.items() if k not in replay.VOLATILE} for d in docs]  # noqa: E731
    assert_snapshot(
        request,
        {
            'info': info,
            'ratings': plain(database.player_ratings.find()),
            'elo_history': plain(database.elo_history.find()),
        },
    )
