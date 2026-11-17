"""Меню и кнопки серверов: сценарии на одинаковых данных сверяются с эталонами (ответы, embed, кнопки, меню,
посты в каналы, правки панели, SSH-команды и база).
"""

from datetime import timedelta

import pytest

from tests import mongo as db

from .conftest import NOW, set_disabled
from .ui import (
    CHANNELS,
    EXT,
    HOSTER,
    MOD,
    Client,
    Interaction,
    click,
    pick,
    run_scenario,
    running,
    shape_view,
    state,
)

PANEL = {'_id': 'panel_message', 'message_id': 42}
ENGINE = '[..] LogInit: (Engine Initialization) Total time: 4.2 seconds'
GUUID = '[2026.02.12-18.22.47:103][  0]LogGameState: GUUID=22EF90D44731D9D9D524AFB96E99634A\n'


@pytest.fixture(autouse=True)
def reset_class_state():
    from lobby_bot.features.servers import views

    views.StopServerView.last_match_end_time = {}
    yield
    views.StopServerView.last_match_end_time = {}


def mixed():
    db.discord_panel_col.insert_one(dict(PANEL))
    state(0, host_id='100', host_name='user100', started_at=NOW - timedelta(minutes=75), host_port=7778)
    state(1, reserved_by_id='300', reserved_by_name='res300', reserved_until=NOW + timedelta(minutes=4))
    state(2, host_id='555', host_name='other', started_at=NOW - timedelta(minutes=3))


@pytest.mark.parametrize("disabled", [set(), {2}, {0, 1, 2}])
@pytest.mark.parametrize("run", [(), (0,), (0, 2), (1,)])
async def test_update_panel_embed(env, monkeypatch, disabled, run):
    set_disabled(disabled)

    async def scenario(f, client, log):
        await f.views.update_panel_embed(client)

    await run_scenario(scenario, env, seed=mixed, responses=running(*run))


async def test_update_panel_without_panel_or_channel(env, monkeypatch):
    async def scenario(f, client, log):
        await f.views.update_panel_embed(client)  # нет id панели
        db.discord_panel_col.insert_one(dict(PANEL))
        client.channels.clear()
        await f.views.update_panel_embed(client)  # нет канала

    await run_scenario(scenario, env)


WANT_HOST = '🎮 Хочу хостить (запрос доступа)'
PANEL_BUTTONS = [
    '🔄 Обновить',
    '🔄 Обновить',
    '⚙️ СОЗДАТЬ СЕРВЕР',
    '🔒 Зарезервировать',
    'ℹ️ Информация о сервере',
    '🗿 Острова',
    '🎮 Хочу хостить (запрос доступа)',
    '🎮 Открыть консоль',
    '👥 Дать доступ к консоли',
]


@pytest.mark.parametrize(
    "user,roles",
    [(100, (HOSTER,)), (300, (HOSTER,)), (400, ()), (400, (HOSTER,)), (400, (HOSTER, MOD, EXT)), (555, (HOSTER, MOD))],
)
@pytest.mark.parametrize("disabled", [set(), {0, 1, 2}])
async def test_panel_buttons(env, monkeypatch, user, roles, disabled):
    set_disabled(disabled)

    async def scenario(f, client, log):
        view = f.views.ServerPanelView()
        log.append(('panel', [(c.label, c.custom_id, c.row, getattr(c, 'url', None)) for c in view.children]))
        for label in PANEL_BUTTONS:
            if disabled and label == WANT_HOST:
                continue  # при отключённых серверах поведение изменено намеренно: test_want_host_all_disabled
            await click(view, label, Interaction(log, client, user, roles))

    await run_scenario(scenario, env, seed=mixed, responses=running(0, 2))


async def test_want_host_all_disabled(env):
    """Все серверы отключены: «Хочу хостить» отвечает как «Создать сервер» и никого не пингует."""
    from lobby_bot.features.servers import service, views

    set_disabled({0, 1, 2})
    log = []
    client = Client(log, CHANNELS)
    await click(views.ServerPanelView(), WANT_HOST, Interaction(log, client))
    assert log == [('response.send', service.SERVERS_UNAVAILABLE_MESSAGE, {'ephemeral': True})]


async def test_panel_without_request_channel(env, monkeypatch):
    async def scenario(f, client, log):
        await click(f.views.ServerPanelView(), '🎮 Хочу хостить (запрос доступа)', Interaction(log, client))

    await run_scenario(scenario, env, channels=())


@pytest.mark.parametrize("user", [100, 300, 400])
@pytest.mark.parametrize("seed", ['mixed', 'busy', 'free', 'ghost'])
async def test_reservation(env, monkeypatch, user, seed):
    def seed_fn():
        if seed == 'mixed':
            mixed()
        if seed == 'ghost':  # хост без имени: сервер считается свободным
            state(2, host_id='7', host_name=None)
        if seed == 'busy':
            for n in range(3):
                state(n, host_id=str(n), host_name=f'h{n}', started_at=NOW)

    async def scenario(f, client, log):
        await click(
            f.views.ReservationRegionView(str(user), f'user{user}'), '❌ Отмена', Interaction(log, client, user)
        )
        await click(
            f.views.ReservationRegionView(str(user), f'user{user}'),
            '🌍 Зарезервировать (Франкфурт)',
            Interaction(log, client, user),
        )
        await click(
            f.views.ReservationRegionView(str(user), f'user{user}'),
            '🌍 Зарезервировать (Франкфурт)',
            Interaction(log, client, user),
        )
        await click(f.views.CancelReservationView(1, str(user)), '❌ Отмена', Interaction(log, client, user))
        for n in (0, 1, 2):
            await click(
                f.views.CancelReservationView(n, str(user)), '🔓 Снять резервацию', Interaction(log, client, user)
            )

    await run_scenario(scenario, env, seed=seed_fn)


async def test_reservation_all_disabled(env, monkeypatch):
    set_disabled({0, 1, 2})

    async def scenario(f, client, log):
        await click(
            f.views.ReservationRegionView('1', 'u'), '🌍 Зарезервировать (Франкфурт)', Interaction(log, client, 1)
        )

    await run_scenario(scenario, env)


async def test_server_info_choice(env, monkeypatch):
    async def scenario(f, client, log):
        data = {'host_id': '100', 'host_name': 'user100', 'started_at': NOW - timedelta(minutes=3), 'host_port': 7779}
        log.append(('embed', f.views.build_server_info_embed(0, data).to_dict()))
        log.append(('embed', f.views.build_server_info_embed(2, {}).to_dict()))
        view = f.views.ServerInfoChoiceView([(0, data), (2, {'host_name': 'x'})], '100')
        log.append(('choice', [(c.label, c.custom_id) for c in view.children]))
        for c in view.children:
            await c.callback(Interaction(log, client))

    await run_scenario(scenario, env)


@pytest.mark.parametrize("user,roles", [(100, (HOSTER,)), (200, (HOSTER,)), (555, (HOSTER, MOD)), (555, (HOSTER,))])
@pytest.mark.parametrize("joined", [True, False])
async def test_stop_server_view(env, monkeypatch, user, roles, joined):
    responses = running(0) + ([('Join succeeded', 'LogNet: Join succeeded: X')] if joined else [])

    async def scenario(f, client, log):
        view = f.views.StopServerView(0, str(user))
        log.append(('view', shape_view(view)))
        await click(view, '🏳️ Победа мирных', Interaction(log, client, user, roles))
        await click(view, '☠️ Победа предателей', Interaction(log, client, user, roles))  # кулдаун
        await click(f.views.StopServerView(0, '999'), '☠️ Победа предателей', Interaction(log, client, user, roles))
        await click(view, '⏹️ Остановить сервер', Interaction(log, client, user, roles))
        await click(f.views.StopServerView(0, '999'), '⏹️ Остановить сервер', Interaction(log, client, user, roles))
        await click(f.views.StopServerView(2, str(user)), '⏹️ Остановить сервер', Interaction(log, client, user, roles))

    await run_scenario(scenario, env, seed=mixed, responses=responses)


async def test_stop_server_helper_error(env, monkeypatch):
    env.fail_connect = True

    async def scenario(f, client, log):
        try:
            await f.views.stop_server(0, Interaction(log, client))
        except Exception as e:
            return repr(e)

    await run_scenario(scenario, env, seed=mixed)


def preset(map_choice='harbor', mods=(), mode='normal', modifiers=None, enabled=False):
    def seed():
        doc = {'user_id': '100', 'map': map_choice, 'mods': list(mods), 'mode': mode, 'region': 'frankfurt'}
        if modifiers is not None:
            doc['custom_modifiers'] = modifiers
            doc['custom_modifiers_enabled'] = enabled
        db.discord_hosts_presets_col.insert_one(doc)

    return seed


SETTINGS_CASES = [
    ((HOSTER,), preset()),
    ((HOSTER, EXT), preset('islands', ['rand_role', 'ability_4', 'islands_10'], 'solo')),
    ((HOSTER,), preset('valley', ['chill', 'islands_11', 'ability_5'], modifiers={'maxplayers': 10}, enabled=True)),
    ((HOSTER, EXT), preset('outpost', ['train_mode', 'console'], modifiers={}, enabled=False)),
]


@pytest.mark.parametrize("roles,seed", SETTINGS_CASES)
async def test_settings_menu(env, monkeypatch, roles, seed):
    async def scenario(f, client, log):
        inter = lambda: Interaction(log, client, 100, roles)  # noqa: E731
        preset_ = await f.views.get_user_preset(client.cog.repo, '100', inter())
        view = await f.views.ServerSettingsView.create(client.cog.repo, '100', 'user100', preset_)
        log.append(('initial', view.get_initial_embed().to_dict()))
        names = {type(c).__name__ for c in view.children}
        if 'GameModeSelect' in names:
            await pick(view, 'GameModeSelect', ['solo'], inter())
            await pick(view, 'GameModeSelect', ['normal'], inter())
        await pick(view, 'RegionSelect', ['frankfurt'], inter())
        for map_choice, mods in [
            ('islands', ['ability_4', 'rand_role']),
            ('islands', ['islands_10']),
            ('harbor', ['ability_4', 'ability_5']),
            ('harbor', ['ability_5', 'ability_4', 'quests']),
            ('valley', []),
            ('islands', ['islands_10', 'islands_11', 'ability_5']),
        ]:
            await pick(view, 'MapSelect', [map_choice], inter())
            await pick(view, 'ModsSelect', mods, inter())
            log.append(('state', view.selected_map, list(view.selected_mods)))
        await click(view, next(c.label for c in view.children if str(getattr(c, 'label', '')).startswith('⚙️')), inter())
        await click(view, '❌ Отмена', inter())

    await run_scenario(scenario, env, seed=seed)


@pytest.mark.parametrize("modifiers", [None, {'maxplayers': 10, 'traitors': 3}])
@pytest.mark.parametrize("enabled", [False, True])
async def test_modifiers_submenu(env, monkeypatch, modifiers, enabled):
    async def scenario(f, client, log):
        inter = lambda: Interaction(log, client, 100, (HOSTER,))  # noqa: E731
        repo = client.cog.repo
        view = await f.views.ServerSettingsView.create(
            repo, '100', 'user100', await f.views.get_user_preset(repo, '100', inter())
        )
        holder = {}
        orig = Interaction.__init__

        def capture(self, *a, **k):
            orig(self, *a, **k)
            send = self.response.edit_message

            async def edit(**kw):
                holder['view'] = kw.get('view')
                await send(**kw)

            self.response.edit_message = edit

        monkeypatch.setattr(Interaction, '__init__', capture)
        mod_label = next(c.label for c in view.children if str(getattr(c, 'label', '')).startswith('⚙️'))
        await click(view, mod_label, inter())
        sub = holder['view']
        labels = [c.label for c in sub.children]
        for label in labels[::-1]:  # Назад, Сбросить, переключить
            await click(view, mod_label, inter())
            await click(holder['view'], label, inter())
        monkeypatch.setattr(Interaction, '__init__', orig)
        log.append(('enabled', view.custom_modifiers_enabled))

    await run_scenario(scenario, env, seed=preset(modifiers=modifiers, enabled=enabled))


LAUNCH = [
    # (ответы ssh, удалено ли меню, отключённые)
    ([('ifconfig.me', '203.0.113.7\n'), ('cat ', ENGINE), ('GUUID=', GUUID)], False, set()),
    ([('ifconfig.me', '203.0.113.7\n'), ('cat ', ENGINE), ('GUUID=', 'nothing')], True, set()),
    ([('ifconfig.me', ''), ('cat ', 'still loading')], False, set()),
    ([('cat ', 'still loading')], True, set()),
    (running(0, 1, 2), False, set()),
    (running(0, 1, 2), True, set()),
    ([], False, {0, 1, 2}),
    ([('ifconfig.me', '203.0.113.7\n'), ('cat ', ENGINE), ('GUUID=', GUUID)], False, {2}),
]


@pytest.mark.parametrize("responses,gone,disabled", LAUNCH)
@pytest.mark.parametrize(
    "seed,roles",
    [
        (preset('islands', ['rand_role', 'relic_mod', 'islands_11']), (HOSTER,)),
        (preset('harbor', ['chill'], 'solo', {'maxplayers': 10, 'traitors': 1}, True), (HOSTER, EXT)),
        (preset('valley', [], modifiers={}, enabled=True), (HOSTER,)),
    ],
)
async def test_launch(env, monkeypatch, responses, gone, disabled, seed, roles):
    set_disabled(disabled)

    def seed_fn():
        seed()
        db.discord_panel_col.insert_one(dict(PANEL))
        state(1, reserved_by_id='100', reserved_by_name='user100', reserved_until=NOW + timedelta(minutes=5))
        state(0, reserved_by_id='300', reserved_by_name='res300', reserved_until=NOW + timedelta(minutes=5))

    async def scenario(f, client, log):
        inter = Interaction(log, client, 100, roles, gone=gone)
        repo = client.cog.repo
        view = await f.views.ServerSettingsView.create(
            repo, '100', 'user100', await f.views.get_user_preset(repo, '100', inter)
        )
        await click(view, '✅ Запустить сервер', inter)

    await run_scenario(scenario, env, seed=seed_fn, responses=responses)


async def test_relicmod_embed(env, monkeypatch):
    async def scenario(f, client, log):
        return f.views.build_relicmod_embed().to_dict()

    await run_scenario(scenario, env)
