"""Команды модераторов ELO сверяются с эталонами в snapshots/: ответы пользователю, посты в лог-канал и всё,
что записано в базу (рейтинги и история ELO).
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from lobby_bot.features.elo import service
from lobby_bot.features.elo.cog import EloAdminCog, EloCog
from lobby_bot.features.elo.repo import EloRepo
from lobby_bot.settings import settings
from tests import mongo as db
from tests.async_mongo import wrap
from tests.snapshot import assert_snapshot

_snap = {}


@pytest.fixture(autouse=True)
def _snap_request(request):
    _snap.update(request=request, count=0)


def snap(value):
    """Эталон под именем теста (с номером, если в тесте их несколько)."""
    _snap['count'] += 1
    request = _snap['request']
    name = request.node.name + (f"-{_snap['count']}" if _snap['count'] > 1 else '')
    assert_snapshot(request, value, name=name)


NOW = datetime(2026, 10, 20, 12, 0)
SEASON = 3
MOD = (settings.moderator_role_id,)


def seed():
    db.seasons_col.insert_one({'season': SEASON, 'is_active': True})
    db.player_ratings_col.insert_many(
        [
            {'steam_id': '76561100', 'season': SEASON, 'current_name': 'Alpha', 'elo': 1600, 'peak_elo': 1650},
            {'steam_id': '76561101', 'season': SEASON, 'current_name': 'Bravo', 'elo': 1500},
            {
                'steam_id': '76561102',
                'season': SEASON,
                'current_name': 'Charlie',
                'elo': 1700,
                'peak_elo': 1700,
                'changed_nickname': 'Чарли',
                'is_penalized': True,
                'penalty_reason': 'очень длинная причина штрафа',
                'win_multiplier': 0.7,
                'loss_multiplier': 1.3,
                'penalty_until': NOW - timedelta(minutes=1),
            },
            {
                'steam_id': '76561103',
                'season': SEASON,
                'current_name': 'Delta',
                'elo': 1450,
                'is_penalized': True,
                'penalty_reason': 'коротко',
                'penalty_until': NOW + timedelta(days=1),
            },
            {
                'steam_id': '76561104',
                'season': SEASON,
                'current_name': 'Echo',
                'elo': 1550,
                'is_penalized': True,
                'penalty_until': None,
            },
            {
                'steam_id': '76561105',
                'season': SEASON,
                'current_name': 'Foxtrot',
                'elo': 1500,
                'is_penalized': True,
                'penalty_until': NOW,
            },  # срок истекает ровно сейчас: снимается
            {'steam_id': '76561100', 'season': 2, 'current_name': 'Alpha', 'elo': 1999},
        ]
    )
    entries = []
    for uuid, when, changes in (
        ('ABCD1234EEEE', NOW - timedelta(days=1), {'76561100': 20, '76561101': -15}),
        ('ABCD9999FFFF', NOW - timedelta(days=2), {'76561102': -11}),
        ('ZZZZ0000', NOW - timedelta(days=3), {'76561100': 7, 'GONE': 5}),
    ):
        for sid, change in changes.items():
            entries.append(
                {
                    'steam_id': sid,
                    'match_uuid': uuid,
                    'season': SEASON,
                    'elo_change': change,
                    'elo_before': 1500,
                    'elo_after': 1500 + change,
                    'match_datetime': when,
                }
            )
    # Много матчей: автокомплит показывает только 25 последних
    entries += [
        {
            'steam_id': '76561101',
            'match_uuid': f'OLD{n:03d}',
            'season': SEASON,
            'elo_change': 1,
            'match_datetime': NOW - timedelta(days=10 + n),
        }
        for n in range(30)
    ]
    entries.append(
        {
            'steam_id': '76561103',
            'match_uuid': 'MANUAL1',
            'season': SEASON,
            'elo_change': 3,
            'manual': True,
            'match_datetime': NOW,
        }
    )
    db.elo_history_col.insert_many(entries)


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(service, "_now", lambda: NOW)


def reset():
    for col in (db.seasons_col, db.player_ratings_col, db.elo_history_col):
        col.delete_many({})
    seed()


def snapshot():
    def clean(docs):
        return sorted(({k: v for k, v in d.items() if k != '_id'} for d in docs), key=repr)

    return clean(db.player_ratings_col.find()), clean(db.elo_history_col.find())


class Log:
    def __init__(self):
        self.embeds = []

    async def send(self, embed):
        self.embeds.append(embed.to_dict())


class Responder:
    def __init__(self, record):
        self.record = record

    async def send_message(self, content=None, **kwargs):
        self.record.append(
            ('send', content, kwargs.get('embed').to_dict() if kwargs.get('embed') else None, kwargs.get('ephemeral'))
        )

    async def defer(self, **kwargs):
        self.record.append(('defer', kwargs))

    async def send(self, content=None, **kwargs):  # followup
        self.record.append(
            (
                'followup',
                content,
                kwargs.get('embed').to_dict() if kwargs.get('embed') else None,
                kwargs.get('ephemeral'),
            )
        )


def interaction(roles=MOD, log=None):
    record = []
    member = SimpleNamespace(
        id=777, display_name='Модер', mention='<@777>', roles=[SimpleNamespace(id=r) for r in roles]
    )
    inter = SimpleNamespace(
        user=member,
        guild=SimpleNamespace(get_member=lambda uid: member),
        client=SimpleNamespace(get_channel=lambda cid: log if cid == settings.elo_log_channel_id else None),
        response=Responder(record),
        followup=Responder(record),
        record=record,
    )
    return inter


def repo() -> EloRepo:
    return EloRepo(
        *(
            wrap(c)
            for c in (
                db.player_ratings_col,
                db.history_player_stats_col,
                db.elo_history_col,
                db.matches_col,
                db.elo_processing_col,
                db.elo_overrides_col,
            )
        )
    )


def admin_cog():
    return EloAdminCog(None, repo(), db.seasons(), settings.elo_log_channel_id, settings.moderator_role_id)


async def run(name, roles=MOD, **kwargs):
    """Команда на свежих данных; ответы, лог-канал и база сверяются с эталоном."""
    reset()
    log = Log()
    inter = interaction(roles, log)
    cog = admin_cog()
    await getattr(cog, name).callback(cog, inter, **kwargs)
    result = (inter.record, log.embeds, snapshot())
    snap(result)
    return result


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(steam_id='76561100', points=25, reason='компенсация'),
        dict(steam_id='76561100', points=-40, reason='штраф'),
        dict(steam_id='nope', points=5, reason='x'),
    ],
)
async def test_elo_adjust(frozen, kwargs):
    record, _log, _ = await run('elo_adjust', **kwargs)
    assert record


async def test_no_rights(frozen):
    for name, kwargs in (
        ('elo_adjust', dict(steam_id='76561100', points=1, reason='x')),
        ('link_twink', dict(twink='76561101', main='76561100')),
        ('elo_revert_match', dict(match_uuid='ABCD')),
        ('elo_penalty_remove', dict(steam_id='76561102')),
    ):
        record, _, _ = await run(name, roles=(), **kwargs)
        assert record[0][1] == '❌ Нет прав.'


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(points=10, reason='турнир', player1='76561100', player2='76561101', player3='nope'),
        dict(points=-5, reason='afk', player1='76561102'),
    ],
)
async def test_elo_adjust_group(frozen, kwargs):
    await run('elo_adjust_group', **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(
            reason='ручной',
            player1='76561100',
            points1=5,
            player2='76561101',
            points2=-7,
            player3='x',
            points3=None,
            player4='nope',
            points4=3,
        ),
        dict(reason='пусто', player1='', points1=5),
    ],
)
async def test_elo_adjust_group_certain(frozen, kwargs):
    await run('elo_adjust_group_certain', **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(twink='76561101', main='76561100'),
        dict(twink='76561101', main='76561101'),
        dict(twink='nope', main='76561100'),
        dict(twink='76561101', main='nope'),
    ],
)
async def test_link_twink(frozen, kwargs):
    await run('link_twink', **kwargs)


@pytest.mark.parametrize("prefix", ['ZZZZ', 'abcd', 'NONE'])  # abcd: два матча с таким началом (как раньше: оба)
async def test_revert_and_undo(frozen, prefix):
    reset()
    log = Log()
    cog = admin_cog()
    records = []
    for _ in range(3):  # откат, отмена отката, снова откат
        inter = interaction(MOD, log)
        await cog.elo_revert_match.callback(cog, inter, match_uuid=prefix)
        records.append(inter.record)
    snap((records, log.embeds, snapshot()))


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(steam_id='76561100', reason='токсик'),
        dict(steam_id='76561100', reason='на час', win_reduction=50, loss_increase=100, duration_minutes=90),
        dict(steam_id='76561100', reason='x', win_reduction=101),
        dict(steam_id='76561100', reason='x', loss_increase=501),
        dict(steam_id='76561100', reason='x', duration_minutes=0),
        dict(steam_id='nope', reason='x'),
    ],
)
async def test_elo_penalty(frozen, kwargs):
    await run('elo_penalty', **kwargs)


@pytest.mark.parametrize("steam_id", ['76561102', '76561100', 'nope'])
async def test_elo_penalty_remove(frozen, steam_id):
    await run('elo_penalty_remove', steam_id=steam_id)


async def test_penalty_expiry(frozen):
    reset()
    log = Log()
    bot = SimpleNamespace(get_channel=lambda cid: log if cid == settings.elo_log_channel_id else None)
    cog = EloCog(bot, repo(), db.seasons(), 1, settings.elo_log_channel_id)
    await cog.penalty_expiry_task.coro(cog)

    snap((log.embeds, snapshot()))
    assert len(log.embeds) == 2  # сняты истёкшие штрафы Charlie и Foxtrot (срок ровно сейчас)


async def test_penalty_expiry_errors_do_not_stop_task(monkeypatch):
    def broken(*args):
        raise ConnectionError("mongo down")

    monkeypatch.setattr(service, "expire_penalties", broken)
    cog = EloCog(SimpleNamespace(get_channel=lambda cid: None), repo(), db.seasons(), 1, 2)
    await cog.penalty_expiry_task.coro(cog)  # не бросает: иначе задача остановилась бы


@pytest.mark.parametrize("current", ['', 'alp', '7656110', 'чар', 'zzz'])
async def test_autocompletes(frozen, current):
    reset()
    cog = EloAdminCog(None, repo(), db.seasons(), 1, 2)
    inter = interaction()
    for autocomplete in (
        cog._elo_adjust_ac,
        cog._link_twink_ac,
        cog._group_ac,
        cog._certain_ac,
        cog._penalty_ac,
        cog._penalty_remove_ac,
    ):
        snap([(c.name, c.value) for c in await autocomplete(inter, current)])

    for prefix in (current.upper()[:2], 'OLD'):
        uuids = await cog._revert_ac(inter, prefix)
        snap([(c.name, c.value) for c in uuids])
    assert len(uuids) == 25


def test_slash_commands_unchanged():
    import discord
    from discord.ext import commands

    bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
    new = {c.name: c.to_dict(bot.tree) for c in EloAdminCog(bot, repo(), db.seasons(), 1, 2).get_app_commands()}
    assert set(new) == {
        'elo_adjust',
        'elo_adjust_group',
        'elo_adjust_group_certain',
        'link_twink',
        'unlink_twink',
        'elo_revert_match',
        'elo_penalty',
        'elo_penalty_remove',
    }
    snap(new)


async def test_unlink_twink(frozen):
    reset()
    inter = interaction(MOD, Log())
    cog = EloAdminCog(None, repo(), db.seasons(), settings.elo_log_channel_id, settings.moderator_role_id)
    await cog.link_twink.callback(cog, inter, twink='76561101', main='76561100')
    inter = interaction(MOD, Log())
    await cog.unlink_twink.callback(cog, inter, twink='76561101')

    assert 'Привязка снята' in inter.record[-1][1]
    doc = db.player_ratings_col.find_one({'steam_id': '76561101', 'season': SEASON})
    assert doc['is_twink'] is False and doc['linked_to'] is None


@pytest.mark.parametrize("twink, expected", [('76561101', 'не привязан как твинк'), ('nope', 'не найден')])
async def test_unlink_twink_refuses(frozen, twink, expected):
    record, _, _ = await run('unlink_twink', twink=twink)
    assert expected in record[-1][1]
    assert db.player_ratings_col.count_documents({'is_twink': False}) == 0  # ничего не записано


async def test_unlink_twink_no_rights(frozen):
    record, _, _ = await run('unlink_twink', roles=(), twink='76561101')
    assert 'Нет прав' in record[-1][1]
