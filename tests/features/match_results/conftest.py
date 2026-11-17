"""Матчи для тестов итогов: рейтинговый (штрафы, твинк, ники, урон) и нерейтинговый."""

import re
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tests import mongo as db

NOW = datetime(2026, 10, 20, 12, 0)
SEASON = 3
RANKED = 'AAAA1111BBBB2222CCCC3333DDDD4444'
CASUAL = 'EEEE5555FFFF6666'
COLLECTIONS = (
    'seasons_col',
    'player_ratings_col',
    'history_player_stats_col',
    'elo_history_col',
    'match_damage_col',
    'player_reports_col',
)

CREW = ['S1', 'S2', 'S3', 'S4', 'TW']  # TW — твинк основного MAIN (основного в матче нет)
THR = ['T1', 'T2']


def _damage(players: dict) -> dict:
    """Урон: по врагам, по своим, себе, от природы и неизвестного источника."""
    raw = {
        'S1': {
            'T1': {'DT_SwordSlash_C': 120.4, 'DT_PistolBullet_C': 60},
            'S2': {'DT_RockBlunt_C': 15},
            'S1': {'DT_Explosive_Charge_C': 30},
        },
        'S2': {'T2': {'DT_Arrow_C': 44.6}, 'T1': {'DT_FistBlunt_C': 5}},
        'T1': {'S1': {'DT_DaggerSlash_C': 200}, 'S3': {'DT_CleaverSlashing_C': 80}, 'T2': {'DT_Blunt_C': 9}},
        'T2': {'S4': {'DT_BearTrap_C': 35}},
        'wildlife': {'S3': {'DT_WolfBite_C': 22, 'DT_BearClaws_C': 18}, 'T2': {'DT_MonsterBite_C': 7}},
        'Unknown': {'S2': {'DT_Falling_C': 12}},
    }
    return {'match_id': players['id'], 'players': players['players'], 'raw_damage': raw}


def seed() -> None:
    db.seasons_col.insert_one({'season': SEASON, 'is_active': True})
    roles = {
        'S1': 'Commander',
        'S2': 'Pilot',
        'S3': 'Medic',
        'S4': '',
        'TW': 'Quartermaster',
        'T1': 'Mechanic',
        'T2': 'Scout',
    }
    for match, ranked in ((RANKED, True), (CASUAL, False)):
        for sid in CREW + THR:
            team = 'Traitors' if sid in THR else 'Crew'
            db.history_player_stats_col.insert_one(
                {
                    'match_uuid': match,
                    'steam_id': sid,
                    'name': f'name_{sid}',
                    'team': team,
                    'won': team == 'Traitors',
                    'role': roles[sid],
                    'match_datetime': datetime(2026, 10, 19, 21, 5),
                    'match_duration_seconds': None if sid == 'S1' else 1863,
                    'is_ranked': ranked,
                    **({'twink_redirected_to': 'MAIN'} if ranked and sid == 'TW' else {}),
                }
            )
        players = {
            sid: {
                'name': f'dmg_{sid}',
                'role': roles[sid],
                'isTraitor': sid in THR,
                'damage_to_traitors': 0 if sid in THR else 50 * len(sid),
                'damage_to_crew': 300 if sid in THR else 0,
            }
            for sid in CREW + THR
            if sid != 'S4'
        }
        players['HOMELANDER'] = {'name': 'bot', 'role': '', 'isTraitor': False}
        players['EXTRA'] = {'name': 'только в уроне', 'role': 'Priest', 'isTraitor': False, 'damage_to_traitors': 3}
        db.match_damage_col.insert_one(_damage({'id': match, 'players': players}))

    elo = {
        'S1': (1600, -12),
        'S2': (1500, -20),
        'S3': (1450, -8),
        'S4': (1700, -30),
        'T1': (1550, 40),
        'T2': (1520, 33),
    }
    for sid, (before, change) in elo.items():
        db.elo_history_col.insert_one(
            {
                'steam_id': sid,
                'match_uuid': RANKED,
                'season': SEASON,
                'elo_before': before,
                'elo_after': before + change,
                'elo_change': change,
                'team': 'Traitors' if sid in THR else 'Crew',
                'won': sid in THR,
                'match_datetime': datetime(2026, 10, 19, 21, 5),
            }
        )
    db.player_ratings_col.insert_many(
        [
            {
                'steam_id': 'S2',
                'season': SEASON,
                'is_penalized': True,
                'win_multiplier': 0.7,
                'loss_multiplier': 1.5,
                'penalty_reason': 'токсичность',
            },
            {'steam_id': 'T1', 'season': SEASON, 'is_penalized': True, 'win_multiplier': 0.5, 'loss_multiplier': 1.0},
            {'steam_id': 'S3', 'season': SEASON, 'changed_nickname': 'Медик Зло'},
            {'steam_id': 'S1', 'season': 1, 'changed_nickname': 'Старый ник'},  # ник из прошлого сезона
            {'steam_id': 'MAIN', 'season': SEASON, 'current_name': 'Основной'},
        ]
    )


@pytest.fixture
def data(monkeypatch):
    from lobby_bot.features.match_results import service

    for name in COLLECTIONS:
        getattr(db, name).delete_many({})
    seed()
    monkeypatch.setattr(service, "now_utc", lambda: NOW.replace(tzinfo=UTC))
    from lobby_bot.features.reports import service as reports_service

    monkeypatch.setattr(reports_service, "now_utc", lambda: NOW)
    yield
    for name in COLLECTIONS:
        getattr(db, name).delete_many({})


class Channel:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class Response:
    def __init__(self):
        self.sent, self.edited = [], []

    async def send_message(self, content=None, **kwargs):
        self.sent.append({'content': content, **kwargs})

    async def edit_message(self, **kwargs):
        self.edited.append(kwargs)


def interaction(log_channel=None):
    user = SimpleNamespace(id=42, display_name='Репортер', mention='<@42>')
    client = SimpleNamespace(get_channel=lambda cid: log_channel)
    return SimpleNamespace(user=user, client=client, response=Response())


def add_main_account_change() -> None:
    """Начисление основному аккаунту за твинка: в такой записи elo_history нет поля team."""
    db.elo_history_col.insert_one(
        {
            'steam_id': 'MAIN',
            'match_uuid': RANKED,
            'season': SEASON,
            'elo_before': 1800,
            'elo_after': 1785,
            'elo_change': -15,
            'twink_penalty': True,
        }
    )


def view_shape(view) -> list:
    """Содержимое view для эталона: тип, custom_id, подписи, пункты меню, ссылки."""
    shape = []
    for item in view.children:
        item = getattr(item, 'item', item)  # DynamicItem: сам компонент внутри
        options = [(o.label, o.value) for o in getattr(item, 'options', [])]
        custom_id = getattr(item, 'custom_id', None)
        if custom_id and re.fullmatch(r'[0-9a-f]{32}', custom_id):
            custom_id = 'auto'  # временные кнопки: discord.py генерирует случайный id
        shape.append(
            (
                type(item).__name__,
                custom_id,
                getattr(item, 'row', None),
                getattr(item, 'label', None),
                getattr(item, 'placeholder', None),
                options,
                getattr(item, 'url', None),
            )
        )
    return shape
