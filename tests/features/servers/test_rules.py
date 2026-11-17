"""Правила, SSH и состояние серверов: результаты сверяются с эталонами в snapshots/. SSH подменён фейком:
в эталонах команды, загрузки и содержимое файлов.
"""

import hashlib
import itertools
from types import SimpleNamespace

import pytest

from lobby_bot.features.console.repo import ConsoleRepo
from lobby_bot.features.servers import api, service, views
from lobby_bot.features.servers.repo import ServersRepo
from lobby_bot.settings import settings
from tests import mongo as db
from tests.async_mongo import wrap
from tests.snapshot import assert_snapshot

from .conftest import NOW, clean_db, snapshot
from .ui import elo_repo

# Функции под старыми именами game/core.py: правила в service, SSH в api, проверки по Interaction в views
shim = SimpleNamespace(
    **{
        **vars(service),
        'execute_ssh_command': api.execute_ssh_command,
        **{
            n: getattr(views, n)
            for n in (
                'check_server_status',
                'extract_match_guuid',
                'check_user_has_permission',
                'is_moderator',
                'has_extended_access',
            )
        },
    }
)

MOD_SETS = [
    [],
    ['rand_role'],
    ['rand_role', 'ability_cast_start'],
    ['ability_cast_start'],
    ['relic_mod'],
    ['chill'],
    ['islands_11'],
    ['islands_10', 'guides'],
    ['rand_role', 'relic_mod', 'islands_11'],
    ['guides', 'quests', 'ability_5', 'ability_4', 'console', 'train_mode', 'ability_upgrade', 'ability_charge'],
    list(service.MODS),
    ['unknown_mod'],
]
CUSTOM = [
    None,
    {},
    {'maxplayers': 10, 'traitors': 3},
    {
        'maxplayers': 12,
        'daysbeforestorm': 7,
        'dayminutes': 16,
        'predatordamage': 0.25,
        'weatherintensity': 3,
        'staminarate': 0.5,
        'fuelburnrate': 2,
        'traitors': 0,
    },
]
# Константы правил серверов (их значения проверяются эталоном)
CONFIG_CONSTANTS = [
    'ADMINS',
    'AVAILABLE_PORTS',
    'COMMANDS_CHANNELS',
    'DEFAULT_MODIFIERS',
    'DISABLED_SERVERS',
    'EXTENDED_ACCESS_ROLE_ID',
    'GAME_MODES',
    'HISTORY_CHANNEL_ID',
    'HOSTER_ROLE_ID',
    'MAPS',
    'MAP_NAMES',
    'MODERATOR_ROLE_ID',
    'MODIFIER_DESCRIPTIONS',
    'MODS',
    'MODS_DB_NAMES',
    'MOD_NAMES',
    'PANEL_CHANNEL_ID',
    'RANKED_MODS',
    'RESERVATION_LOG_CHANNEL_ID',
    'SERVERS',
    'SERVERS_UNAVAILABLE_MESSAGE',
    'SSH_CONFIGS',
]


def plain(value):
    """Множества в отсортированные списки: порядок set зависит от хеш-сида."""
    if isinstance(value, (set, frozenset)):
        return sorted(plain(v) for v in value)
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def test_constants_unchanged(request):
    assert_snapshot(request, {n: plain(getattr(service, n)) for n in CONFIG_CONSTANTS})


def test_build_game_command_matrix(request):
    commands = [
        service.build_game_command(map_choice, list(mods), mode, server, custom, port)
        for map_choice, mods, mode, server, custom, port in itertools.product(
            service.MAPS, MOD_SETS, service.GAME_MODES, service.SERVERS, CUSTOM, [None, 7779]
        )
    ]
    assert len(commands) > 2000
    # Все команды целиком дали бы эталон в несколько мегабайт: хеш всех и полные первые
    digest = hashlib.sha256('\n'.join(map(repr, commands)).encode()).hexdigest()
    assert_snapshot(request, {'cases': len(commands), 'sha256': digest, 'first': commands[:12]})


def test_ranked_and_db_names_matrix(request):
    rows = [
        (mods, mode, map_choice, service.is_ranked_match(mods, mode, map_choice), service.get_mods_for_db(mods))
        for mods, mode, map_choice in itertools.product(MOD_SETS, ['normal', 'solo'], [None, *list(service.MAPS)])
    ]
    assert_snapshot(request, {'rows': rows, 'all_disabled': service.all_servers_disabled()})


@pytest.mark.parametrize("minutes", [0, 5, 59, 60, 61, 60 * 23 + 59, 60 * 24, 60 * 24 * 3 + 125])
def test_uptime_text(frozen, request, minutes):
    from datetime import timedelta

    started = NOW - timedelta(minutes=minutes, seconds=7)
    assert service.get_uptime_text(None) is None
    assert_snapshot(request, service.get_uptime_text(started))


def member_interaction(role_ids):
    member = SimpleNamespace(id=5, roles=[SimpleNamespace(id=r) for r in role_ids])
    return SimpleNamespace(user=member, guild=SimpleNamespace(get_member=lambda uid: member))


@pytest.mark.parametrize("roles", [(), ('hoster',), ('moderator',), ('extended',), ('hoster', 'moderator', 'extended')])
def test_role_checks(request, roles):
    ids = {
        'hoster': settings.hoster_role_id,
        'moderator': settings.moderator_role_id,
        'extended': settings.extended_access_role_id,
    }
    inter = member_interaction([ids[r] for r in roles])
    assert_snapshot(
        request,
        {
            name: getattr(shim, name)(inter)
            for name in ('check_user_has_permission', 'is_moderator', 'has_extended_access')
        },
    )
    nobody = SimpleNamespace(user=SimpleNamespace(id=1), guild=SimpleNamespace(get_member=lambda uid: None))
    assert shim.is_moderator(nobody) is False


def repo() -> ServersRepo:
    return ServersRepo(
        wrap(db.discord_hosts_notify_col),
        wrap(db.discord_hosts_presets_col),
        wrap(db.discord_panel_col),
        wrap(db.game_matches_col),
        wrap(db.player_ratings_col),
        wrap(db.history_player_stats_col),
    )


async def _db_scenario():
    from datetime import timedelta

    m, r = service, repo()
    console = ConsoleRepo(wrap(db.console_access_col), wrap(db.player_locations_col))

    async def preset_fn(user_id, inter):
        return await views.get_user_preset(r, user_id, inter)

    out = []
    sid = service.SERVERS[0]['server_id']
    out.append([await m.get_next_port(r, sid) for _ in range(4)])
    db.discord_hosts_notify_col.update_one({'server': sid}, {'$set': {'current_port': 1234}})
    out.append(await m.get_next_port(r, sid))
    await m.set_current_host(r, sid, 'u1', 'Хост', port=7778)
    await m.set_current_host(r, service.SERVERS[1]['server_id'], 'u2', 'Без порта')
    out.append(await m.get_current_host(r, sid))
    out.append(await m.get_current_host(r, 'nope'))
    out.append(await m.reserve_server(r, 2, 'u3', 'Резерв'))
    out.append(await m.get_reservation(r, 2))
    out.append(
        [
            await m.is_server_occupied(r, 2, 'u3'),
            await m.is_server_occupied(r, 2, 'other'),
            await m.is_server_occupied(r, 1, 'x'),
        ]
    )
    db.discord_hosts_notify_col.update_one(
        {'server': service.SERVERS[1]['server_id']}, {'$set': {'reserved_until': NOW - timedelta(seconds=1)}}
    )
    out.append(await m.get_reservation(r, 1))
    await m.clear_reservation(r, 2)
    out.append(await m.get_reservation(r, 2))
    db.console_access_col.insert_one({'server_num': 0, 'user_ids': ['9']})
    await m.clear_current_host(r, console, sid)
    await m.clear_current_host(r, console, 'unknown_server')
    out.append(await preset_fn('u1', None))
    out.append(await preset_fn('newbie', member_interaction([settings.extended_access_role_id])))
    await m.save_user_preset(r, 'u1', 'valley', ['quests'], 'solo', 'frankfurt')
    out.append(await preset_fn('u1', None))
    out.append(await preset_fn('u1', member_interaction([settings.extended_access_role_id])))
    db.discord_hosts_presets_col.insert_one({'user_id': 'partial'})
    out.append(await preset_fn('partial', None))
    out.append(
        [
            await m.get_user_modifiers(r, 'u1'),
            await m.get_user_modifiers_raw(r, 'u1'),
            await m.get_user_modifiers_raw(r, 'none'),
        ]
    )
    await m.save_user_modifiers(r, 'u1', {'maxplayers': 9})
    out.append([await m.get_user_modifiers(r, 'u1'), await m.get_user_modifiers_raw(r, 'u1')])
    await m.set_user_modifiers_enabled(r, 'u1', True)
    await m.set_user_modifiers_enabled(r, 'u9', False)
    out.append([await m.get_user_modifiers(r, 'u1'), await m.get_user_modifiers(r, 'u9')])
    await m.save_match_info(r, 'GUUID1', ['rand_role', 'chill'], 'normal', 'harbor')
    await m.save_match_info(r, 'GUUID2', ['rand_role'], 'normal', 'harbor')
    out.append(await m.get_panel_message_id(r))
    await m.save_panel_message_id(r, 555)
    await m.save_panel_message_id(r, 556)
    out.append(await m.get_panel_message_id(r))
    return out


def without_ids(value):
    """Без _id документов: ObjectId разные в каждом прогоне."""
    if isinstance(value, dict):
        return {k: without_ids(v) for k, v in value.items() if k != '_id'}
    if isinstance(value, (list, tuple)):
        return [without_ids(v) for v in value]
    return value


async def test_state_and_presets(frozen, request):
    clean_db()
    result = (await _db_scenario(), snapshot())
    clean_db()
    assert_snapshot(request, without_ids(result))


@pytest.mark.parametrize(
    "responses,fail",
    [
        ([('screen -list', 'There is a screen on: 123.sdgame')], False),
        ([('screen -list', 'No Sockets found')], False),
        ([('GUUID=', '[2026.02.12-18.22.47:103][  0]LogGameState: GUUID=22EF90D44731D9D9D524AFB96E99634A\n')], False),
        ([('GUUID=', 'garbage')], False),
        ([], True),
    ],
)
async def test_ssh_status_and_guuid(ssh, monkeypatch, request, responses, fail):
    monkeypatch.setattr(service, "DISABLED_SERVERS", set())
    ssh.responses, ssh.fail_connect = responses, fail
    status = await shim.check_server_status(1)
    try:
        guuid = await shim.extract_match_guuid(2)
    except OSError as e:
        guuid = f'error: {e}'
    assert_snapshot(request, {'status': status, 'guuid': guuid, 'ssh': ssh.log})


async def test_disabled_server_is_not_contacted(ssh):
    assert await shim.check_server_status(0) is False
    assert ssh.log == []


def test_execute_ssh_command(ssh, request):
    ssh.responses = [('echo', 'hi\n')]
    assert shim.execute_ssh_command('echo hi', 1) == ('hi\n', '')
    assert_snapshot(request, ssh.log)


def _seed_cache():
    db.seasons_col.insert_one({'season': 3, 'is_active': True})
    db.player_ratings_col.insert_many(
        [
            {
                'steam_id': 'A',
                'season': 3,
                'elo': 1700,
                'total_games': 20,
                'traitors_games': 2,
                'current_name': 'Альфа',
                'role_stats': {'Commander': {'games': 3, 'wins': 1}},
                'is_calibrated': True,
            },
            {
                'steam_id': 'B',
                'season': 3,
                'elo': 1600,
                'total_games': 8,
                'traitors_games': 4,
                'changed_nickname': 'Бэ',
            },
            {'steam_id': 'C', 'season': 3, 'elo': 1650, 'total_games': 0, 'traitors_games': 0},
            {'steam_id': 'D', 'season': 3, 'elo': 1500, 'total_games': 4, 'traitors_games': 1},
            {'season': 3, 'elo': 2000},  # без steam_id
            {'steam_id': 'OLD', 'season': 2, 'elo': 3000},
        ]
    )
    db.elo_overrides_col.insert_many(
        [{'steam_id': 'A', 'bonus': 10}, {'steam_id': 'D', 'bonus': -60}, {'steam_id': 'GHOST', 'bonus': 40}]
    )
    rows = []
    for m, (p1, p2, won) in enumerate(
        [('A', 'B', True), ('A', 'B', False), ('B', 'A', True), ('C', 'D', True), ('A', 'C', False), ('D', 'D', True)]
    ):
        rows += [
            {
                'match_uuid': f'm{m}',
                'steam_id': p,
                'name': f'n{p}',
                'won': won,
                'team': 'Traitors',
                'is_ranked': True,
                'is_valid_match': True,
            }
            for p in (p1, p2)
        ]
    # Одинаковый винрейт у пар с разным числом игр: порядок по играм, а не по порядку матчей
    for m, (p1, p2) in enumerate([('E', 'G'), ('E', 'F'), ('E', 'F')]):
        rows += [
            {
                'match_uuid': f't{m}',
                'steam_id': p,
                'name': p,
                'won': True,
                'team': 'Traitors',
                'is_ranked': True,
                'is_valid_match': True,
            }
            for p in (p1, p2)
        ]
    rows += [
        {
            'match_uuid': 'three',
            'steam_id': p,
            'name': p,
            'won': True,
            'team': 'Traitors',
            'is_ranked': True,
            'is_valid_match': True,
        }
        for p in 'ABC'
    ]
    db.history_player_stats_col.insert_many(rows)


@pytest.mark.parametrize("fail,template", [(False, True), (False, False), (True, True)])
async def test_elo_cache_upload(ssh, request, fail, template):
    clean_db()
    _seed_cache()
    folder = service.SERVERS[1]['folder']
    if template:
        ssh.files[f'{folder}/scripts/traitor_weight_template.js'] = 'var AdditionalScore = ADDITIONAL_SCORE;'
    ssh.fail_connect = fail
    ok = await service.generate_elo_cache(repo(), elo_repo(), db.seasons(), 1)
    best = await service.generate_traitor_best_winrates(repo(), 1)
    clean_db()
    assert_snapshot(request, {'ok': ok, 'best': best, 'ssh': ssh.log})
    if not fail:
        assert any(e[0] == 'put' and e[1].endswith('elo_cache.json') for e in ssh.log)
        assert any(e[0] == 'write' for e in ssh.log) == template


async def test_pair_winrates(ssh, request):
    clean_db()
    _seed_cache()
    result = await service.calculate_traitor_pair_winrates(repo())
    clean_db()
    assert_snapshot(request, result)
