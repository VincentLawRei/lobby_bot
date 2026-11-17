"""Фейки для сценариев консоли: SSH-туннель в консоль, interaction с записью ответов, база."""

import json
import re
from datetime import datetime
from types import SimpleNamespace

import paramiko
import pytest
from discord.ui import TextInput

from lobby_bot.settings import settings
from tests import mongo as db

NOW = datetime(2026, 10, 20, 12, 0)
MOD = settings.moderator_role_id


class ConsoleWorld:
    def __init__(self):
        self.sent = []  # (hostname, команда)
        self.fail = False


class _Channel:
    def __init__(self, world, host):
        self.world, self.host = world, host

    def send(self, data):
        self.world.sent.append((self.host, json.loads(data.decode('utf-16-le'))))

    def close(self):
        pass


class FakeSSH:
    def __init__(self, world):
        self.world = world

    def set_missing_host_key_policy(self, policy):
        pass

    def connect(self, **kwargs):
        if self.world.fail:
            raise OSError("ssh down")
        self.host = kwargs['hostname']

    def get_transport(self):
        world, host = self.world, self.host
        return SimpleNamespace(open_channel=lambda kind, dest_addr, src_addr, timeout: _Channel(world, host))

    def close(self):
        pass


@pytest.fixture
def console_ssh(monkeypatch):
    world = ConsoleWorld()
    monkeypatch.setattr(paramiko, "SSHClient", lambda: FakeSSH(world))
    return world


@pytest.fixture
def frozen(monkeypatch):
    from lobby_bot.features.console import service

    monkeypatch.setattr(service, "_now", lambda: NOW)


def clean():
    db.console_access_col.delete_many({})
    db.player_locations_col.delete_many({})


def snapshot():
    return {
        name: sorted(({k: v for k, v in d.items() if k != '_id'} for d in getattr(db, name).find()), key=repr)
        for name in ('console_access_col', 'player_locations_col')
    }


def shape(obj):
    """Сравнимый вид view / modal / embed (без случайных custom_id)."""
    if obj is None:
        return None
    items = []
    for item in getattr(obj, 'children', []):
        item = getattr(item, 'item', item)
        cid = getattr(item, 'custom_id', None)
        if cid and re.fullmatch(r'[0-9a-f]{32}', cid):
            cid = 'auto'
        # У TextInput атрибут label устарел в discord.py 2.6: подпись берём из того, что уходит в Discord
        label = item.to_component_dict().get('label') if isinstance(item, TextInput) else getattr(item, 'label', None)
        items.append(
            (
                type(item).__name__,
                cid,
                getattr(item, 'row', None),
                label,
                getattr(item, 'placeholder', None),
                getattr(item, 'disabled', None),
                [(o.label, o.value, o.description, o.default) for o in getattr(item, 'options', [])],
                getattr(item, 'min_values', None),
                getattr(item, 'max_values', None),
            )
        )
    return (type(obj).__name__, getattr(obj, 'title', None), getattr(obj, 'timeout', None), items)


class Recorder:
    def __init__(self):
        self.log = []

    async def send_message(self, content=None, **kw):
        self.log.append(('send', content, shape(kw.get('view')), kw.get('ephemeral')))

    async def edit_message(self, **kw):
        self.log.append(('edit', kw.get('content'), shape(kw.get('view')) if 'view' in kw else 'keep'))

    async def send_modal(self, modal):
        self.log.append(('modal', shape(modal)))
        self.last_modal = modal


class Channel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append((content, shape(kw.get('view'))))


def interaction(user_id=100, roles=(), channel=None):
    user = SimpleNamespace(
        id=user_id, display_name=f'User{user_id}', mention=f'<@{user_id}>', roles=[SimpleNamespace(id=r) for r in roles]
    )
    rec = Recorder()
    return SimpleNamespace(
        user=user,
        response=rec,
        client=SimpleNamespace(get_channel=lambda cid: channel if cid == settings.share_channel_id else None),
    )
