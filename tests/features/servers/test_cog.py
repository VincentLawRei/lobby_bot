"""Команды и фоновые задачи серверов: сценарии на одинаковых данных сверяются с эталонами (ответы, посты
в каналы, SSH-команды и база).
"""

from datetime import timedelta

import pytest

from lobby_bot.settings import settings

from .conftest import NOW, set_disabled
from .ui import (
    CMD_CHANNEL,
    EXT,
    HOSTER,
    HOSTS,
    MOD,
    Ctx,
    Interaction,
    click,
    play,
    run_scenario,
    running,
    state,
)

ADMIN = settings.admin_ids[0]
LOG = 'Join succeeded'


def hosting(user_id=100, started=NOW - timedelta(minutes=20)):
    def seed():
        state(0, host_id=str(user_id), host_name=f'user{user_id}', started_at=started, host_port=7778)
        state(1, host_id='555', host_name='other', started_at=NOW - timedelta(minutes=3))

    return seed


@pytest.mark.parametrize(
    "user,roles,channel,disabled,seed",
    [
        (100, (HOSTER,), 1, set(), None),  # чужой канал
        (100, (HOSTER,), CMD_CHANNEL, {0, 1, 2}, None),  # все серверы выключены
        (100, (), CMD_CHANNEL, set(), None),  # нет роли хостера
        (100, (HOSTER,), CMD_CHANNEL, set(), 'hosting'),  # уже хостит
        (100, (HOSTER, MOD), CMD_CHANNEL, set(), 'hosting'),  # модератору можно второй
        (100, (HOSTER, EXT), CMD_CHANNEL, set(), 'preset'),  # пресет и расширенный доступ
        (100, (HOSTER,), CMD_CHANNEL, {1}, None),
    ],
)
async def test_srv(env, monkeypatch, user, roles, channel, disabled, seed):
    def seed_fn():
        if seed == 'hosting':
            hosting()()
        if seed == 'preset':
            from tests import mongo as db

            db.discord_hosts_presets_col.insert_one(
                {
                    'user_id': '100',
                    'map': 'islands',
                    'mods': ['rand_role', 'ability_4'],
                    'mode': 'solo',
                    'region': 'frankfurt',
                    'custom_modifiers': {'maxplayers': 9},
                    'custom_modifiers_enabled': True,
                }
            )

    set_disabled(disabled)

    async def scenario(f, client, log):
        await f.cmd('server_slash', Interaction(log, client, user, roles, channel))

    await run_scenario(scenario, env, seed=seed_fn)


@pytest.mark.parametrize(
    "roles,run,channel",
    [
        ((HOSTER,), (0, 1), 1),
        ((), (0, 1), CMD_CHANNEL),
        ((HOSTER,), (), CMD_CHANNEL),
        ((HOSTER,), (0, 1), CMD_CHANNEL),  # свой один -> сразу подтверждение
        ((HOSTER, MOD), (0, 1), CMD_CHANNEL),  # модератор: выбор из двух
        ((HOSTER, MOD), (), CMD_CHANNEL),
        ((HOSTER,), (1,), CMD_CHANNEL),  # запущен только чужой
    ],
)
async def test_stop_and_confirm(env, monkeypatch, roles, run, channel):
    async def scenario(f, client, log):
        inter = Interaction(log, client, 100, roles, channel)
        await f.cmd('stop_slash', inter)
        sent = [e for e in log if e[0] == 'response.send']
        if sent and sent[-1][2].get('view'):
            # Пройти дальше по первому варианту
            view_name = sent[-1][2]['view'][0]
            return view_name

    await run_scenario(scenario, env, seed=hosting(), responses=running(*run))


async def test_stop_confirm_flow(env, monkeypatch):
    async def scenario(f, client, log):
        from .ui import Interaction

        data = {'host_id': '100', 'host_name': 'user100', 'started_at': NOW - timedelta(minutes=20)}
        view = f.views.StopConfirmView(0, data, '100')
        log.append(('text', view.get_confirmation_text()))
        await click(view, '❌ Отмена', Interaction(log, client))
        view = f.views.StopConfirmView(0, data, '100')
        await click(view, '✅ Да, остановить', Interaction(log, client))
        choice = f.views.StopServerChoiceView([(0, data), (1, None)], '100')
        log.append(('choice', [c.label for c in choice.children]))
        await choice.children[1].callback(Interaction(log, client))

    await run_scenario(scenario, env, seed=hosting())


async def test_static_commands(env, monkeypatch):
    async def scenario(f, client, log):
        await f.cmd('relicmod_slash', Interaction(log, client))
        await f.cmd('useful_things_slash', Interaction(log, client))

    await run_scenario(scenario, env)


@pytest.mark.parametrize("author,panel_channel", [(1, True), (ADMIN, False), (ADMIN, True)])
async def test_gamepanel(env, monkeypatch, author, panel_channel):
    set_disabled({2})

    def seed():
        hosting()()
        state(2, reserved_by_id='7', reserved_by_name='res', reserved_until=NOW + timedelta(minutes=5))

    channels = (settings.panel_channel_id,) if panel_channel else ()

    async def scenario(f, client, log):
        await f.cmd('gamepanel', Ctx(log, author))

    await run_scenario(scenario, env, seed=seed, channels=channels)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {'maxplayers': 13, 'traitors': -1, 'fuelburnrate': 0.05, 'dayminutes': 4},
        {'maxplayers': 0, 'daysbeforestorm': 8, 'predatordamage': 3.5, 'weatherintensity': 0.2, 'staminarate': 4},
        {'maxplayers': 10, 'traitors': 3},
        {
            'daysbeforestorm': 2,
            'dayminutes': 16,
            'predatordamage': 0.25,
            'weatherintensity': 3,
            'staminarate': 1.5,
            'fuelburnrate': 2,
            'traitors': 0,
            'maxplayers': 12,
        },
    ],
)
async def test_usermodifiers(env, monkeypatch, kwargs):
    def seed():
        from tests import mongo as db

        db.discord_hosts_presets_col.insert_one(
            {'user_id': '100', 'custom_modifiers': {'dayminutes': 7, 'traitors': 4}}
        )

    async def scenario(f, client, log):
        await f.cmd('usermodifiers_slash', Interaction(log, client), **kwargs)
        await f.cmd('usermodifiers_slash', Interaction(log, client, user_id=200), **kwargs)

    await run_scenario(scenario, env, seed=seed)


@pytest.mark.parametrize("cmd", ['crew_win_slash', 'traitors_win_slash'])
@pytest.mark.parametrize(
    "roles,run,channel,joined",
    [
        ((HOSTER,), (0,), 1, True),
        ((), (0,), CMD_CHANNEL, True),
        ((HOSTER,), (), CMD_CHANNEL, True),
        ((HOSTER,), (0, 1), CMD_CHANNEL, True),
        ((HOSTER,), (0,), CMD_CHANNEL, False),
        ((HOSTER, MOD), (0, 1), CMD_CHANNEL, True),
        ((HOSTER, MOD), (), CMD_CHANNEL, True),
    ],
)
async def test_force_win(env, monkeypatch, cmd, roles, run, channel, joined):
    """Экраны подтверждения и выбора сервера как в старом коде. Сам выбор доступных серверов
    исправлен и проверяется отдельно: test_force_win_server_choice."""
    responses = running(*run) + ([('Join succeeded', 'LogNet: Join succeeded: Player')] if joined else [])

    async def scenario(f, client, log):
        winner = 1 if cmd == 'crew_win_slash' else 2
        data = {'host_id': '100', 'host_name': 'user100', 'started_at': NOW - timedelta(minutes=20)}
        confirm = f.views.ForceWinConfirmView(0, data, '100', winner)
        log.append(('text', confirm.get_confirmation_text()))
        await click(confirm, '✅ Да, завершить', Interaction(log, client, 100, roles))
        await click(f.views.ForceWinConfirmView(0, None, '100', winner), '❌ Отмена', Interaction(log, client))
        choice = f.views.ForceWinServerChoiceView([(0, data), (1, None)], '100', winner)
        log.append(('choice', [(c.label, c.custom_id) for c in choice.children]))
        await choice.children[1].callback(Interaction(log, client))

    await run_scenario(scenario, env, seed=hosting(), responses=responses)


@pytest.mark.parametrize("cmd,winner", [('crew_win_slash', 1), ('traitors_win_slash', 2)])
@pytest.mark.parametrize(
    "roles,run,expected",
    [
        # Хост сервера 1 (номер 0) завершает свой матч. Регрессия: раньше статус брался с сервера 0, а хост
        # с сервера, который первым идёт в SERVERS (номер 2), и бот отвечал «Нет доступных серверов».
        ((HOSTER,), (0,), 'confirm'),
        ((HOSTER,), (0, 1), 'confirm'),
        ((HOSTER,), (1,), 'none'),  # запущен только чужой сервер
        ((HOSTER, MOD), (0, 1), 'choice'),  # модератор выбирает из всех запущенных
        ((HOSTER, MOD), (), 'none'),
    ],
)
async def test_force_win_server_choice(env, cmd, winner, roles, run, expected):
    """Статус, хост и номер сервера для завершения матча берутся с одного и того же сервера."""

    async def scenario(f, client, log):
        await f.cmd(cmd, Interaction(log, client, 100, roles, CMD_CHANNEL))

    _, log, _, _ = await play(scenario, env, seed=hosting(), responses=running(*run))
    assert len(log) == 1
    _kind, content, kw = log[0]
    view = kw.get('view')
    if expected == 'confirm':
        assert view[0] == 'ForceWinConfirmView'
        assert f'Сервер 1 ({HOSTS[0]}:7777)' in content and 'Хост: user100' in content
    elif expected == 'choice':
        assert view[0] == 'ForceWinServerChoiceView'
        assert [(b[1], b[3]) for b in view[2]] == [
            ('force_win_choice_0', f'Сервер 1 ({HOSTS[0]}:7777) — user100'),
            ('force_win_choice_1', f'Сервер 2 ({HOSTS[1]}:7777) — other'),
        ]
    else:
        assert view is None and content.startswith('❌ Нет')


# ============ Фоновые задачи ============
async def test_reservation_cleanup(env, monkeypatch):
    def seed():
        state(0, reserved_by_id='1', reserved_by_name='old', reserved_until=NOW - timedelta(seconds=1))
        state(1, reserved_by_id='2', reserved_by_name='fresh', reserved_until=NOW + timedelta(minutes=1))
        state(2, reserved_by_id='3', reserved_until=NOW - timedelta(minutes=9))

    async def scenario(f, client, log):
        await f.task('reservation_cleanup_task')

    await run_scenario(scenario, env, seed=seed)


@pytest.mark.parametrize("channels", [None, ()])
async def test_auto_stop(env, monkeypatch, channels):
    def seed():
        state(0, host_id='1', host_name='long', started_at=NOW - timedelta(hours=1, seconds=1))
        state(1, host_id='2', host_name='short', started_at=NOW - timedelta(minutes=59))
        state(2, host_id='3', host_name='nostart')
        from tests import mongo as db

        db.discord_panel_col.insert_one({'_id': 'panel_message', 'message_id': 42})

    async def scenario(f, client, log):
        await f.task('auto_stop_task')

    kw = {} if channels is None else {'channels': channels}
    await run_scenario(scenario, env, seed=seed, responses=running(0, 1), **kw)


@pytest.mark.parametrize("count", ['0', '1', '3', '', 'garbage'])
async def test_failed_start_check(env, monkeypatch, count):
    def seed():
        state(0, host_id='1', host_name='stuck', started_at=NOW - timedelta(minutes=5, seconds=1))
        state(1, host_id='2', host_name='young', started_at=NOW - timedelta(minutes=4))
        from tests import mongo as db

        db.discord_panel_col.insert_one({'_id': 'panel_message', 'message_id': 42})

    async def scenario(f, client, log):
        await f.task('failed_start_check_task')

    await run_scenario(scenario, env, seed=seed, responses=[*running(0), ('Login request', count)])


async def test_panel_update_task(env, monkeypatch):
    def seed():
        hosting()()
        from tests import mongo as db

        db.discord_panel_col.insert_one({'_id': 'panel_message', 'message_id': 42})

    async def scenario(f, client, log):
        await f.task('panel_update_task')

    await run_scenario(scenario, env, seed=seed, responses=running(0, 2))


@pytest.mark.parametrize(
    "panel,channel,disabled", [(True, True, set()), (False, False, set()), (False, True, set()), (False, True, {1})]
)
async def test_create_initial_panel(env, monkeypatch, panel, channel, disabled):
    set_disabled(disabled)

    def seed():
        hosting()()
        state(2, host_id=None, host_name=None, started_at=None)
        from tests import mongo as db

        if panel:
            db.discord_panel_col.insert_one({'_id': 'panel_message', 'message_id': 42})

    channels = (settings.panel_channel_id,) if channel else ()

    async def scenario(f, client, log):
        await f.initial_panel()

    await run_scenario(scenario, env, seed=seed, responses=running(0, 2), channels=channels)
