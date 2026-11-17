"""Правила расчёта ELO на маленьких примерах (чистые функции) и ручные бонусы в MongoDB."""

from datetime import datetime

import pytest

from lobby_bot.features.elo import service

from . import replay
from .conftest import make_match

NOW = datetime(2026, 10, 1)
SEASON = 1


def ratings_for(rows, **overrides):
    """Рейтинги игроков матча: новые, с правками {steam_id: {поле: значение}}."""
    result = {}
    for r in rows:
        doc = service.new_rating(r['steam_id'], r['game_uid'], r['name'], SEASON, NOW)
        doc.update(overrides.get(r['steam_id'], {}))
        result[r['steam_id']] = doc
    return result


def changes(calc):
    return {p.steam_id: p.match_fields['elo_change'] for p in calc.players}


# ---------- Формулы ----------
def test_expected_score_is_symmetric():
    assert service.calculate_expected_score(1500, 1500) == 0.5
    assert service.calculate_expected_score(1600, 1400) + service.calculate_expected_score(1400, 1600) == 1


@pytest.mark.parametrize(
    "streak,is_traitor,expected",
    [
        (1, True, 1.0),
        (2, True, 1.05),
        (5, True, 1.15),
        (9, True, 1.25),
        (2, False, 1.0),
        (3, False, 1.05),
        (6, False, 1.10),
        (7, False, 1.20),
    ],
)
def test_winstreak_multiplier(streak, is_traitor, expected):
    assert service.get_winstreak_multiplier(streak, is_traitor) == expected


def test_elo_change_rules():
    even = dict(player_elo=1500, team_avg_elo=1500, opponent_avg_elo=1500, is_calibration=False)
    assert service.calculate_elo_change(**even, won=True, is_traitor_player=False) == 16
    assert service.calculate_elo_change(**even, won=False, is_traitor_player=False) == -16
    assert service.calculate_elo_change(**even, won=True, is_traitor_player=True) == round(16 * 1.6)
    assert service.calculate_elo_change(**even, won=False, is_traitor_player=True) == round(-16 * 0.6)
    calibration = {**even, 'is_calibration': True}
    assert service.calculate_elo_change(**calibration, won=True, is_traitor_player=False) == 25
    # винстрик только при победе
    assert service.calculate_elo_change(**even, won=True, is_traitor_player=False, winstreak=7) == round(16 * 1.2)
    assert service.calculate_elo_change(**even, won=False, is_traitor_player=False, winstreak=7) == -16


# ---------- Матч ----------
def test_regular_match_updates_counters():
    rows = make_match('m', [('A', 'Crew'), ('B', 'Crew'), ('C', 'Traitors')], traitors_won=True, when=NOW)
    calc = service.calculate_match('m', rows, ratings_for(rows), {}, SEASON, NOW)

    a = next(p for p in calc.players if p.steam_id == 'A')
    assert a.rating_fields['losses'] == 1 and a.rating_fields['crew_losestreak'] == 1
    assert a.rating_fields['calibration_games'] == 1
    c = next(p for p in calc.players if p.steam_id == 'C')
    assert c.rating_fields['traitors_wins'] == 1 and c.rating_fields['traitor_winstreak'] == 1
    assert c.rating_fields['role_stats'] == {'Traitor': {'games': 1, 'wins': 1}}
    assert calc.match['avg_elo_crew'] == 1500 and calc.match['players_count'] == 3


def test_calibration_ends_after_ten_games():
    rows = make_match('m', [('A', 'Crew'), ('B', 'Traitors')], traitors_won=False, when=NOW)
    calc = service.calculate_match('m', rows, ratings_for(rows, A={'calibration_games': 9}), {}, SEASON, NOW)
    assert calc.players[0].rating_fields['is_calibrated'] is True


def test_penalty_multiplier():
    rows = make_match('m', [('A', 'Crew'), ('B', 'Traitors')], traitors_won=True, when=NOW)
    plain = changes(service.calculate_match('m', rows, ratings_for(rows), {}, SEASON, NOW))
    penalized = changes(
        service.calculate_match('m', rows, ratings_for(rows, A={'loss_multiplier': 1.5}), {}, SEASON, NOW)
    )
    assert penalized['A'] == round(plain['A'] * 1.5)


def test_twink_with_main_in_match():
    """Твинк не получает ELO, основной при поражении теряет вдвое больше."""
    rows = make_match('m', [('MAIN', 'Crew'), ('TW', 'Crew'), ('X', 'Traitors')], traitors_won=True, when=NOW)
    plain = changes(service.calculate_match('m', rows, ratings_for(rows), {}, SEASON, NOW))
    calc = service.calculate_match(
        'm', rows, ratings_for(rows, TW={'is_twink': True, 'linked_to': 'MAIN'}), {}, SEASON, NOW
    )
    result = changes(calc)
    assert result['TW'] == 0
    assert result['MAIN'] == round(plain['MAIN'] * 2)
    assert calc.mains == []


def test_twink_without_main_gives_elo_to_main():
    rows = make_match('m', [('TW', 'Traitors'), ('X', 'Crew')], traitors_won=True, when=NOW)
    main = service.new_rating('MAIN', 'game', 'main', SEASON, NOW) | {'elo': 1700, 'calibration_games': 10}
    calc = service.calculate_match(
        'm', rows, ratings_for(rows, TW={'is_twink': True, 'linked_to': 'MAIN'}), {'MAIN': main}, SEASON, NOW
    )

    tw = next(p for p in calc.players if p.steam_id == 'TW')
    assert tw.rating_fields is None and tw.match_fields['twink_redirected_to'] == 'MAIN'
    assert calc.match['avg_elo_traitors'] == 1700  # в среднем команды твинк считается с ELO основного
    [m] = calc.mains
    assert m.steam_id == 'MAIN' and m.elo_after - m.elo_before == m.history['elo_change'] > 0


def test_one_team_match_is_not_calculated():
    rows = make_match('m', [('A', 'Crew')], traitors_won=False, when=NOW)
    assert service.calculate_match('m', rows, ratings_for(rows), {}, SEASON, NOW) is None


async def test_process_match_writes_everything():
    database = replay.new_database()
    rows = make_match('m', [('A', 'Crew'), ('B', 'Traitors')], traitors_won=True, when=NOW)
    database.history_player_stats.insert_many(rows)
    repo = replay.make_repo(database)
    database.elo_processing.insert_one({'match_uuid': 'm', 'processed': False})

    assert await service.process_queued_match(repo, 'm', SEASON)

    assert database.player_ratings.count_documents({'season': SEASON}) == 2
    assert database.elo_history.count_documents({'match_uuid': 'm'}) == 2
    assert database.matches.find_one({'match_uuid': 'm'})['season'] == SEASON
    assert database.elo_processing.find_one({'match_uuid': 'm'})['processed'] is True
    assert await repo.pending_matches() == []


async def test_unprocessable_match_stays_in_queue():
    database = replay.new_database()
    repo = replay.make_repo(database)
    database.elo_processing.insert_one({'match_uuid': 'nothing', 'processed': False})

    assert not await service.process_queued_match(repo, 'nothing', SEASON)
    assert [m['match_uuid'] for m in await repo.pending_matches()] == ['nothing']


def _queued_match(database):
    rows = make_match('m', [('A', 'Crew'), ('B', 'Crew'), ('C', 'Traitors')], traitors_won=True, when=NOW)
    database.history_player_stats.insert_many(rows)
    database.elo_processing.insert_one({'match_uuid': 'm', 'processed': False})


async def test_crash_while_writing_does_not_count_match_twice():
    """Процесс упал посреди записи: следующий проход дописывает тот же расчёт, ELO не начисляется дважды."""
    clean_db = replay.new_database()
    _queued_match(clean_db)
    assert await service.process_queued_match(replay.make_repo(clean_db), 'm', SEASON)

    crashed_db = replay.new_database()
    _queued_match(crashed_db)
    repo = replay.make_repo(crashed_db)
    real_write = repo.add_match_elo_history
    calls = []

    async def write_then_crash(doc):
        calls.append(doc['steam_id'])
        await real_write(doc)
        if len(calls) == 2:
            raise ConnectionError("база отвалилась")

    repo.add_match_elo_history = write_then_crash
    with pytest.raises(ConnectionError):
        await service.process_queued_match(repo, 'm', SEASON)
    assert await repo.pending_matches()  # матч остался в очереди

    repo.add_match_elo_history = real_write
    assert await service.process_queued_match(repo, 'm', SEASON)

    assert replay.diff(replay.snapshot(clean_db), replay.snapshot(crashed_db)) == []
    assert crashed_db.elo_history.count_documents({'match_uuid': 'm'}) == 3
    assert 'elo_plan' not in crashed_db.elo_processing.find_one({'match_uuid': 'm'})
    assert await repo.pending_matches() == []


async def test_search_treats_input_as_text():
    """Ввод в автодополнении ищется как текст: спецсимволы regex не ломают запрос и не расширяют поиск."""
    database = replay.new_database()
    repo = replay.make_repo(database)
    for sid, name in [('1', 'a.b'), ('2', 'axb'), ('3', 'x(y')]:
        database.player_ratings.insert_one({'steam_id': sid, 'current_name': name, 'season': SEASON})
    database.elo_history.insert_one({'match_uuid': 'AB12', 'steam_id': '1', 'season': SEASON})

    assert [p['steam_id'] for p in (await repo.search_players(SEASON, 'a.b'))] == ['1']
    assert [p['steam_id'] for p in (await repo.search_players(SEASON, 'x('))] == ['3']
    assert await repo.match_entries('.', SEASON) == []
    assert len(await repo.match_entries('ab', SEASON)) == 1  # регистр по-прежнему не важен


# ---------- Ручные бонусы ----------
async def test_overrides_from_database():
    database = replay.new_database()
    database.elo_overrides.insert_many([{'steam_id': '111', 'bonus': 25}, {'steam_id': '222', 'bonus': -60}])
    assert await service.elo_overrides(replay.make_repo(database)) == {'111': 25, '222': -60}
