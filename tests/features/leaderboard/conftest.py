"""Данные лидерборда в общей базе в памяти (db.py) и фейки Discord для команд и сообщений."""

import random
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from tests import mongo as db

NOW = datetime(2026, 10, 20, 12, 0)
SEASON = 3
COLLECTIONS = (
    'seasons_col',
    'player_ratings_col',
    'history_player_stats_col',
    'elo_history_col',
    'discord_panel_col',
    'profile_views_col',
)


def _season_data(rnd: random.Random, season: int, pool: list[str], matches: int, start: datetime):
    """Матчи сезона: строки игроков, история ELO и рейтинги, согласованные между собой."""
    stats, history = [], []
    ratings = {
        sid: {
            'steam_id': sid,
            'season': season,
            'current_name': f'Игрок {sid}',
            'elo': 1500,
            'peak_elo': 1500,
            'ranked_games': 0,
            'wins': 0,
            'losses': 0,
            'crew_games': 0,
            'crew_wins': 0,
            'traitors_games': 0,
            'traitors_wins': 0,
            'calibration_games': 0,
            'is_calibrated': False,
            'win_rate': 0.0,
        }
        for sid in pool
    }
    duo = pool[:2]  # частая пара предателей: дуэты и серии
    for m in range(matches):
        when = start + timedelta(hours=11 * m)
        players = rnd.sample(pool, 8)
        traitors = set(duo) if m % 3 == 0 else set(players[:2])
        players = list(dict.fromkeys(sorted(traitors) + players))[:8]  # sorted: порядок set зависит от хеш-сида
        traitors_won = rnd.random() < (0.75 if traitors == set(duo) else 0.4)
        uuid = f's{season}m{m:03d}'
        for sid in players:
            team = 'Traitors' if sid in traitors else 'Crew'
            won = (team == 'Traitors') == traitors_won
            r = ratings[sid]
            change = rnd.randint(8, 30) * (1 if won else -1)
            r['elo'] += change
            r['ranked_games'] += 1
            r['wins' if won else 'losses'] += 1
            key = 'traitors' if team == 'Traitors' else 'crew'
            r[f'{key}_games'] += 1
            r[f'{key}_wins'] += int(won)
            r['calibration_games'] = min(10, r['calibration_games'] + 1)
            r['is_calibrated'] = r['calibration_games'] >= 10
            r['win_rate'] = round(r['wins'] / r['ranked_games'], 3)
            r['last_match_date'] = when
            stats.append(
                {
                    'match_uuid': uuid,
                    'steam_id': sid,
                    'name': r['current_name'],
                    'team': team,
                    'won': won,
                    'match_outcome': 'Traitors Won' if traitors_won else 'Crew Won',
                    'match_datetime': when,
                    'deaths': rnd.choice([0, 0, 1, 2, 3]),
                    'season': season,
                    'is_ranked': True,
                    'is_valid_match': True,
                }
            )
            history.append(
                {
                    'steam_id': sid,
                    'match_uuid': uuid,
                    'season': season,
                    'elo_after': r['elo'],
                    'elo_change': change,
                    'team': team,
                    'won': won,
                    'match_datetime': when,
                }
            )
    return stats, history, list(ratings.values())


def seed_leaderboard(seed: int = 3) -> None:
    rnd = random.Random(seed)
    pool = [f'7656{i:04d}' for i in range(14)]
    db.seasons_col.insert_many(
        [{'season': 1, 'is_active': False}, {'season': 2, 'is_active': False}, {'season': SEASON, 'is_active': True}]
    )
    for season, matches, start in (
        (1, 15, NOW - timedelta(days=200)),
        (2, 30, NOW - timedelta(days=90)),
        (SEASON, 45, NOW - timedelta(days=22)),
    ):
        stats, history, ratings = _season_data(rnd, season, pool if season > 1 else pool[:10], matches, start)
        db.history_player_stats_col.insert_many(stats)
        db.elo_history_col.insert_many(history)
        db.player_ratings_col.insert_many(ratings)

    sid = pool[0]
    # Ручная правка, decay и начисление за твинка: в истории матчей не показываются, на графике есть
    db.elo_history_col.insert_many(
        [
            {
                'steam_id': sid,
                'match_uuid': 'MANUAL',
                'season': SEASON,
                'elo_after': 1600,
                'elo_change': 10,
                'manual': True,
                'won': False,
                'match_datetime': NOW - timedelta(days=1),
            },
            {
                'steam_id': sid,
                'match_uuid': 's3m001',
                'season': SEASON,
                'elo_after': 1610,
                'elo_change': 10,
                'twink_penalty': True,
                'match_datetime': NOW - timedelta(hours=3),
            },
        ]
    )
    db.player_ratings_col.update_one({'steam_id': pool[1], 'season': SEASON}, {'$set': {'is_penalized': True}})
    db.player_ratings_col.update_many({'steam_id': pool[2]}, {'$set': {'changed_nickname': 'Ник из модерки'}})
    # Игроки с одной игрой: таблица длиннее страницы и топа канала
    db.player_ratings_col.insert_many(
        [
            {
                'steam_id': f'X{n:02d}',
                'season': SEASON,
                'current_name': f'Случайный {n}',
                'elo': 1400 + n,
                'ranked_games': 1,
                'wins': 0,
                'losses': 1,
                'win_rate': 0.0,
                'calibration_games': 1,
                'crew_games': 1,
                'last_match_date': NOW - timedelta(days=n),
            }
            for n in range(15)
        ]
    )
    # Неоткалиброванный игрок
    db.player_ratings_col.insert_one(
        {
            'steam_id': 'NEW',
            'season': SEASON,
            'current_name': 'Новичок',
            'elo': 1510,
            'ranked_games': 2,
            'wins': 1,
            'losses': 1,
            'win_rate': 0.5,
            'calibration_games': 2,
            'last_match_date': NOW - timedelta(minutes=20),
        }
    )


@pytest.fixture
def clean_db():
    for name in COLLECTIONS:
        getattr(db, name).delete_many({})
    yield
    for name in COLLECTIONS:
        getattr(db, name).delete_many({})


@pytest.fixture
def frozen_now(monkeypatch):
    """Одно и то же «сейчас»: эталоны не зависят от даты запуска."""
    from lobby_bot.features.leaderboard import service

    monkeypatch.setattr(service, "now_utc", lambda: NOW)
    return NOW


@pytest.fixture
def data(clean_db, frozen_now):
    seed_leaderboard()


# ---------- Фейки Discord ----------
class Response:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def send_message(self, content=None, **kwargs):
        self.sent.append({'content': content, **kwargs})

    async def edit_message(self, **kwargs):
        self.edited.append(kwargs)


def interaction(role_ids=(), user_id=10):
    member = SimpleNamespace(id=user_id, roles=[SimpleNamespace(id=r) for r in role_ids])
    guild = SimpleNamespace(get_member=lambda uid: member if uid == user_id else None)
    return SimpleNamespace(user=member, guild=guild, response=Response())


class FakeMessage:
    def __init__(self, message_id, author=None, title=''):
        self.id = message_id
        self.author = author
        self.embeds = [SimpleNamespace(title=title)] if title else []
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeChannel:
    def __init__(self, messages=(), history=()):
        self.messages = {m.id: m for m in messages}
        self._history = list(history)
        self.sent = []

    async def fetch_message(self, message_id):
        if message_id not in self.messages:
            import discord

            raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'Unknown Message')
        return self.messages[message_id]

    async def send(self, **kwargs):
        msg = FakeMessage(1000 + len(self.sent))
        self.sent.append(kwargs)
        return msg

    async def history(self, limit):
        for msg in self._history[:limit]:
            yield msg
