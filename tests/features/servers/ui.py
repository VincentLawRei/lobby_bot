"""Фейки Discord для сценариев серверов: interaction, каналы, сообщения с записью всего, что ушло.

`play` прогоняет сценарий на чистой базе, `run_scenario` вдобавок сверяет с эталоном в snapshots/ результат,
ответы, SSH-лог и базу.
"""

import re
from types import SimpleNamespace

import discord

from lobby_bot.features.servers.service import SERVERS
from lobby_bot.settings import settings
from tests import mongo as db
from tests.async_mongo import wrap
from tests.snapshot import assert_snapshot

from .conftest import NOW, SCENARIO, clean_db, snapshot

HOSTER = settings.hoster_role_id
MOD = settings.moderator_role_id
EXT = settings.extended_access_role_id
CMD_CHANNEL = settings.commands_channel_ids[0]
HOSTS = {n: getattr(settings, f"game_server_{n + 1}_host") for n in range(3)}


def shape_embed(e):
    if e is None:
        return None
    return (
        e.title,
        e.description,
        e.color.value if e.color else None,
        e.url,
        [(f.name, f.value, f.inline) for f in e.fields],
        e.footer.text if e.footer else None,
    )


def shape_view(v):
    if v is None:
        return None
    items = []
    for item in v.children:
        cid = getattr(item, 'custom_id', None)
        if cid and re.fullmatch(r'[0-9a-f]{32}', cid):
            cid = 'auto'
        items.append(
            (
                type(item).__name__,
                cid,
                item.row,
                getattr(item, 'label', None),
                str(getattr(item, 'style', None)),
                getattr(item, 'url', None),
                item.disabled,
                getattr(item, 'placeholder', None),
                [(o.label, o.value, o.description, str(o.emoji), o.default) for o in getattr(item, 'options', [])],
                getattr(item, 'min_values', None),
                getattr(item, 'max_values', None),
            )
        )
    return (type(v).__name__, v.timeout, items)


def norm(kw: dict) -> dict:
    out = {}
    for k, v in kw.items():
        if k == 'embed':
            v = shape_embed(v)
        elif k == 'view':
            v = shape_view(v)
        out[k] = v
    return out


class Message:
    _ids = iter(range(900, 10**6))

    def __init__(self, log, where, mid=None):
        self.log, self.where = log, where
        self.id = mid if mid is not None else next(Message._ids)

    async def edit(self, **kw):
        self.log.append((self.where, 'message.edit', self.id, norm(kw)))


class Channel:
    def __init__(self, log, cid):
        self.log, self.id = log, cid

    async def send(self, content=None, **kw):
        self.log.append((self.id, 'send', content, norm(kw)))
        return Message(self.log, self.id, mid=7000 + len(self.log))

    async def fetch_message(self, mid):
        self.log.append((self.id, 'fetch', mid))
        return Message(self.log, self.id, mid)


def elo_repo():
    from lobby_bot.features.elo.repo import EloRepo

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


def make_cog(client):
    """ServersCog на mongomock: views берут репозитории у него через client.get_cog."""
    from lobby_bot.features.console.repo import ConsoleRepo
    from lobby_bot.features.servers import cog
    from lobby_bot.features.servers.repo import ServersRepo

    repo = ServersRepo(
        wrap(db.discord_hosts_notify_col),
        wrap(db.discord_hosts_presets_col),
        wrap(db.discord_panel_col),
        wrap(db.game_matches_col),
        wrap(db.player_ratings_col),
        wrap(db.history_player_stats_col),
    )
    return cog.ServersCog(
        client, repo, ConsoleRepo(wrap(db.console_access_col), wrap(db.player_locations_col)), elo_repo(), db.seasons()
    )


class Client:
    def __init__(self, log, channel_ids):
        self.channels = {cid: Channel(log, cid) for cid in channel_ids}
        self.cog = make_cog(self)

    def get_channel(self, cid):
        return self.channels.get(cid)

    def get_cog(self, name):
        return self.cog if name == type(self.cog).__name__ else None


class Response:
    def __init__(self, log, gone=False):
        self.log, self.gone = log, gone

    async def send_message(self, content=None, **kw):
        self.log.append(('response.send', content, norm(kw)))

    async def edit_message(self, **kw):
        if self.gone:
            raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'Unknown Message')
        self.log.append(('response.edit', norm(kw)))

    async def defer(self, **kw):
        self.log.append(('response.defer', kw))


class Interaction:
    def __init__(self, log, client, user_id=100, roles=(HOSTER,), channel_id=CMD_CHANNEL, gone=False):
        self.log = log
        self.client = client
        self.channel_id = channel_id
        self.user = SimpleNamespace(
            id=user_id, name=f'user{user_id}', display_name=f'User {user_id}', mention=f'<@{user_id}>'
        )
        member = SimpleNamespace(roles=[SimpleNamespace(id=r) for r in roles])
        self.guild = SimpleNamespace(get_member=lambda uid: member if uid == user_id else None)
        self.response = Response(log, gone)
        self.followup = SimpleNamespace(send=self._followup)
        self.message = Message(log, 'interaction', mid=1)

    async def _followup(self, content=None, **kw):
        self.log.append(('followup.send', content, norm(kw)))

    async def edit_original_response(self, **kw):
        self.log.append(('edit_original', norm(kw)))


class Ctx:
    def __init__(self, log, author_id):
        self.log, self.author = log, SimpleNamespace(id=author_id)

    async def send(self, content=None, **kw):
        self.log.append(('ctx.send', content, norm(kw)))


def child(view, label=None, kind=None):
    """Элемент view по подписи кнопки или по типу (Select)."""
    for item in view.children:
        if label is not None and getattr(item, 'label', None) == label:
            return item
        if kind is not None and isinstance(item, kind) and label is None:
            return item
    raise LookupError(label or kind)


async def click(view, label, inter):
    await child(view, label).callback(inter)


async def pick(view, kind, values, inter):
    select = next(c for c in view.children if type(c).__name__ == kind)
    select._values = list(values)
    await select.callback(inter)


class Feature:
    """Доступ сценария к фиче: views и Cog с командами и задачами."""

    def __init__(self, client):
        from lobby_bot.features.servers import views

        self.views = views
        self.cog = client.cog

    async def cmd(self, name, *args, **kwargs):
        return await getattr(self.cog, name).callback(self.cog, *args, **kwargs)

    async def task(self, name):
        return await getattr(self.cog, name).coro(self.cog)

    async def initial_panel(self):
        return await self.cog.create_initial_panel()


CHANNELS = (
    settings.panel_channel_id,
    settings.history_channel_id,
    settings.reservation_log_channel_id,
    settings.host_request_channel_id,
)


async def play(scenario, ssh, seed=None, responses=(), channels=CHANNELS):
    """Сценарий scenario(feature, client, log) на чистой базе. Возвращает (результат, ответы, SSH-лог, база)."""
    from lobby_bot.features.servers import views

    clean_db()
    views.ServerPanelView.last_refresh_time = None
    Message._ids = iter(range(900, 10**6))  # ID новых сообщений не зависят от предыдущих тестов
    # Активный сезон заранее: иначе seasons создаёт его с реальным временем
    db.seasons_col.insert_one({'season': 1, 'is_active': True, 'started_at': NOW})
    if seed:
        seed()
    ssh.log.clear()
    ssh.responses = list(responses)
    log = []
    client = Client(log, channels)
    out = await scenario(Feature(client), client, log)
    result = (out, log, list(ssh.log), snapshot())
    clean_db()
    return result


async def run_scenario(scenario, ssh, seed=None, responses=(), channels=CHANNELS):
    """Сценарий и сверка с эталоном: результат сценария, ответы в Discord, SSH-лог и база после него."""
    out, log, ssh_log, database = await play(scenario, ssh, seed, responses, channels)
    SCENARIO['count'] += 1
    name = SCENARIO['request'].node.name + (f"-{SCENARIO['count']}" if SCENARIO['count'] > 1 else '')
    assert_snapshot(SCENARIO['request'], {'result': out, 'log': log, 'ssh': ssh_log, 'db': database}, name=name)
    return out, log, ssh_log, database


def state(server_num, **fields):
    db.discord_hosts_notify_col.update_one({"server": SERVERS[server_num]["server_id"]}, {"$set": fields}, upsert=True)


def running(*server_nums):
    """SSH-ответы: эти серверы запущены (screen есть), остальные нет."""
    return [('screen -list', 'There is a screen on: 1.sdgame', HOSTS[n]) for n in server_nums]
