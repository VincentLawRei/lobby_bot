"""Консоль: сценарии на одинаковых данных сверяются с эталонами в snapshots/ (ответы пользователю: текст,
кнопки, меню, модальные окна; команды, ушедшие в консоль сервера; посты в канал шаринга; база).
"""

import pytest

from lobby_bot.features.console import cog, service
from lobby_bot.features.console.repo import ConsoleRepo
from lobby_bot.features.servers.service import ADMINS
from tests import mongo as db
from tests.async_mongo import wrap
from tests.snapshot import assert_snapshot

from .conftest import MOD, Channel, clean, interaction, snapshot

R = ConsoleRepo(wrap(db.console_access_col), wrap(db.player_locations_col))


def select_of(view):
    from discord.ui import Select

    return next(c for c in view.children if isinstance(c, Select))


def submit(modal, **values):
    for name, value in values.items():
        getattr(modal, name)._value = value
    return modal


def pick(select, *values):
    select._values = list(values)
    return select


@pytest.fixture
def run(request, console_ssh):
    """Сценарий scenario(channel) на чистой базе и сверка с эталоном."""

    async def run_scenario(scenario, seed=None):
        clean()
        if seed:
            seed()
        console_ssh.sent.clear()
        channel = Channel()
        out = await scenario(channel)
        result = (out, list(console_ssh.sent), channel.sent, snapshot())
        clean()
        assert_snapshot(request, {'result': result[0], 'console': result[1], 'shared': result[2], 'db': result[3]})
        return result

    return run_scenario


async def test_access_and_server_select(console_ssh, frozen, run):
    async def scenario(channel):
        out = []
        view = cog.ConsoleServerSelectView([0, 2], R)
        from .conftest import shape

        out.append(shape(view))
        users = {'nobody': interaction(1), 'mod': interaction(2, roles=(MOD,)), 'admin': interaction(int(ADMINS[0]))}
        for inter in users.values():
            await pick(cog.ConsoleServerSelectView([0, 2], R).children[0], '2').callback(inter)
            out.append(inter.response.log)

        host = interaction(5)
        access_view = cog.ConsoleAccessServerSelectView([1], R)
        await pick(access_view.children[0], '1').callback(host)
        out.append(host.response.log)
        grant = interaction(5)
        u7, u8 = interaction(7).user, interaction(8).user
        await pick(cog.ConsoleAccessView(1, R).children[0], u7, u8).callback(grant)
        out.append(grant.response.log)
        granted = interaction(7)
        await pick(cog.ConsoleServerSelectView([1], R).children[0], '1').callback(granted)
        out.append(granted.response.log)
        out.append(
            [
                await service.has_console_access(R, interaction(8).user, 1),
                await service.has_console_access(R, interaction(8).user, 0),
            ]
        )
        clear = interaction(5)
        await pick(cog.ConsoleAccessView(1, R).children[0]).callback(clear)
        out.append(clear.response.log)
        await service.grant_console_access(R, 2, ['9'], '5')
        await service.revoke_console_access(R, 2)
        return out

    await run(scenario)


async def test_control_panel(console_ssh, frozen, run):
    async def scenario(channel):
        out = []
        view = cog.ServerConsoleControlView(1, R)
        for button in (view.minhealth_btn, view.teleport_btn, view.giveitem_btn, view.restore_btn):
            inter = interaction()
            await button.callback(inter)
            out.append(inter.response.log)
        inter = interaction()
        await pick(select_of(view), 'Medic').callback(inter)
        out.append((inter.response.log, view.selected_target, select_of(view).placeholder))
        for button in (view.minhealth_btn, view.teleport_btn, view.giveitem_btn, view.restore_btn, view.broadcast_btn):
            inter = interaction()
            await button.callback(inter)
            out.append(inter.response.log)
            if button is view.broadcast_btn:
                done = interaction()
                await submit(inter.response.last_modal, msg='Всем привет').on_submit(done)
                out.append(done.response.log)
        return out

    await run(scenario)


async def test_console_errors(console_ssh, frozen, run):
    async def scenario(channel):
        out = [service.send_console_command(99, {'op': 'x'})]
        console_ssh.fail = True
        view = cog.ServerConsoleControlView(0, R)
        await pick(select_of(view), 'Scout').callback(interaction())
        inter = interaction()
        await view.restore_btn.callback(inter)
        out.append(inter.response.log)
        inter = interaction()
        await submit(cog.MinHealthCustomModal(0, 'Scout'), value='50').on_submit(inter)
        out.append(inter.response.log)
        console_ssh.fail = False
        return out

    await run(scenario)


async def test_minhealth(console_ssh, frozen, run):
    async def scenario(channel):
        out = []
        for value in ('25.5', 'abc', '0'):
            inter = interaction()
            await submit(cog.MinHealthCustomModal(2, 'Commander'), value=value).on_submit(inter)
            out.append(inter.response.log)
        return out

    await run(scenario)


def seed_global():
    db.player_locations_col.insert_one(
        {
            'user_id': '__global__',
            'locations': [
                {'name': 'Скала с зарядом #2', 'coords': '1,2,3'},
                {'name': 'Маяк', 'coords': '-100,200,300'},
            ],
        }
    )


async def test_teleport(console_ssh, frozen, run):
    async def scenario(channel):
        from .conftest import shape

        out = [shape(await cog.TeleportView.create(0, 'Scout', R))]
        repeat = interaction(42)
        await cog.RepeatTeleportButton(0, 'Scout', R).callback(repeat)
        out.append(repeat.response.log)
        for x, y, z in (('10.6', '-20.4', '30'), ('a', '1', '2'), ('50001', '0', '0'), ('-50000', '50000', '0')):
            inter = interaction(42)
            await submit(cog.TeleportModal(0, 'Scout', R), x=x, y=y, z=z).on_submit(inter)
            out.append(inter.response.log)
        for label in ('Маяк', 'Скала с зарядом #1', 'нет такой'):
            inter = interaction(42)
            await pick(
                cog.LocationPresetSelect(1, 'Quartermaster', R, await service.get_global_locations(R)), label
            ).callback(inter)
            out.append(inter.response.log)
        repeat = interaction(42)
        await cog.RepeatTeleportButton(2, 'Guard', R).callback(repeat)
        out.append(repeat.response.log)
        inter = interaction(42)
        await cog.TeleportCustomButton(0, 'Scout', R).callback(inter)
        out.append(inter.response.log)
        return out

    await run(scenario, seed=seed_global)


async def test_my_locations_and_sharing(console_ssh, frozen, run):
    async def scenario(channel):
        out = []
        start = interaction(42)
        await cog.MyLocationsButton(0, 'Scout', R).callback(start)
        out.append(start.response.log)
        for name, x, y, z in (
            ('База', '1', '2', '3'),
            ('Плохая', 'x', '1', '1'),
            ('Далеко', '60000', '0', '0'),
            ('База', '4', '5', '6'),
        ):
            inter = interaction(42)
            await submit(cog.AddLocationModal(0, 'Scout', R), name=f'  {name} ', x=x, y=y, z=z).on_submit(inter)
            out.append(inter.response.log)
        save = interaction(42)
        await cog.SaveLastButton(0, 'Scout', R).callback(save)
        out.append(save.response.log)
        await submit(cog.TeleportModal(0, 'Scout', R), x='7', y='8', z='9').on_submit(interaction(42))
        save = interaction(42)
        await cog.SaveLastButton(0, 'Scout', R).callback(save)
        out.append(save.response.log)
        done = interaction(42)
        await submit(save.response.last_modal, name='Последняя').on_submit(done)
        out.append(done.response.log)

        view = await cog.MyLocationsView.create(0, 'Scout', 42, R, is_mod=True)
        out.append(view.render_text())
        my_select, delete_select, share_select = view.children[:3]
        for name in ('База', 'нет'):
            inter = interaction(42)
            await pick(my_select, name).callback(inter)
            out.append(inter.response.log)
        inter = interaction(42, channel=channel)
        await pick(share_select, 'Последняя').callback(inter)
        out.append(inter.response.log)
        inter = interaction(42)
        await pick(share_select, 'Последняя').callback(inter)  # канал не найден
        out.append(inter.response.log)
        inter = interaction(42)
        await pick(delete_select, 'База').callback(inter)
        out.append(inter.response.log)

        shared = cog.ShareLocationButton('Точка|с чертой', '1,-2,3', R)
        out.append((shared.item.custom_id, shared.item.label))
        long_name = cog.ShareLocationButton('Очень длинное название точки ' * 4, '5,5,5', R)
        out.append((long_name.item.custom_id, long_name.item.label))
        inter = interaction(77)
        await shared.callback(inter)
        out.append(inter.response.log)
        for n in range(cog.MAX_LOCATIONS):
            await service.add_user_location(R, 78, f'p{n}', '0,0,0')
        inter = interaction(78)
        await shared.callback(inter)
        out.append(inter.response.log)
        inter = interaction(78)
        await submit(cog.AddLocationModal(0, 'Scout', R), name='p3', x='1', y='1', z='1').on_submit(inter)
        out.append(inter.response.log)  # перезапись существующей точки при лимите
        return out

    await run(scenario)


async def test_global_locations(console_ssh, frozen, run):
    async def scenario(channel):
        out = []
        for roles in ((), (MOD,)):
            inter = interaction(42, roles=roles)
            await cog.GlobalManageButton(0, 'Scout', R).callback(inter)
            out.append(inter.response.log)
            inter = interaction(42, roles=roles)
            await cog.AddGlobalButton(0, 'Scout', R).callback(inter)
            out.append(inter.response.log)
        for name, x in (('Новая', '5'), ('Плохая', 'q'), ('Скала с зарядом #1', '1')):
            inter = interaction(42, roles=(MOD,))
            await submit(cog.AddGlobalModal(0, 'Scout', R), name=name, x=x, y='1', z='1').on_submit(inter)
            out.append(inter.response.log)
        out.append([await service.get_global_locations(R), await service.get_global_db_locations(R)])
        view = await cog.GlobalManageView.create(0, 'Scout', R)
        for roles in ((), (MOD,)):
            inter = interaction(42, roles=roles)
            await pick(view.children[0], 'Новая').callback(inter)
            out.append(inter.response.log)
        return out

    await run(scenario, seed=seed_global)


async def test_give_items(console_ssh, frozen, run):
    async def scenario(channel):
        from .conftest import shape

        view = cog.ItemSelectView(1, 'Pilot')
        out = [shape(view)]
        inter = interaction()
        await view.give_btn.callback(inter)
        out.append(inter.response.log)
        steps = [(0, '3'), (1, 'Еда'), (2, 'Чай (Tea)'), (1, 'Ресурсы / прочее'), (2, 'Топливо (Fuel)')]
        for index, value in steps:
            inter = interaction()
            await pick(view.children[index], value).callback(inter)
            out.append(inter.response.log)
        inter = interaction()
        await view.give_btn.callback(inter)
        out.append(inter.response.log)
        return out

    await run(scenario)


def test_constants_and_helpers(request):
    from lobby_bot.features.console import repo, service

    constants = {
        name: getattr(repo if name == 'GLOBAL_KEY' else service, name)
        for name in (
            'CONSOLE_PORT',
            'COORD_LIMIT',
            'MAX_LOCATIONS',
            'SHARE_CHANNEL_ID',
            'RESTORE_PROPS',
            'RESTORE_VALUE',
            'ITEM_CATEGORIES',
            'ITEMS',
            'LOCATIONS',
            'GLOBAL_KEY',
        )
    }
    ranges = {
        coords: service.coords_out_of_range(coords)
        for coords in ('1,2,3', '1,2', 'a,b,c', '50000,-50000,0', '50000.1,0,0', '')
    }
    assert_snapshot(
        request, {'constants': constants, 'out_of_range': ranges, 'fmt': service.fmt_coords(1.5, -2.5, 3.49)}
    )
