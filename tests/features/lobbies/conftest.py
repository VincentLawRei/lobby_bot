"""Фейки для тестов мониторинга лобби: лобби в формате API, каналы Discord, база в памяти."""

from types import SimpleNamespace

import discord
import mongomock
import pytest

from lobby_bot.features.lobbies.cog import LobbiesCog
from lobby_bot.features.lobbies.repo import LobbiesRepo
from lobby_bot.settings import settings
from tests.async_mongo import wrap

BOT_ID = 999


def make_lobby(name, steam, game, total, free, listening, language="RU", members=(), public=(), age=12, session="s1"):
    return {
        "lobby_host_name": name,
        "lobby_native_session_id": session,
        "total_players": total,
        "open_public_players": free,
        "time_from_creation": age,
        "owner": game,
        "public_players_ids": list(public),
        "language": language,
        "is_server_listening": listening,
        "leader_id": [steam, game],
        "lobby_players": [{"id": m[0], "diff_id": m[1], "name": m[2]} for m in members],
    }


class Message:
    def __init__(self, channel, message_id, content):
        self.channel, self.id, self.content = channel, message_id, content
        self.author = SimpleNamespace(id=BOT_ID)

    async def edit(self, *, content):
        self.content = content
        self.channel.log.append(("edit", self.id))

    async def delete(self):
        self.channel.messages.pop(self.id)
        self.channel.log.append(("delete", self.id))


class Channel:
    def __init__(self, guild):
        self.guild, self.messages, self.log = guild, {}, []

    async def send(self, content):
        message = Message(self, len(self.log) + 1, content)
        self.messages[message.id] = message
        self.log.append(("send", message.id))
        return message

    async def fetch_message(self, message_id):
        if message_id not in self.messages:
            raise discord.NotFound(SimpleNamespace(status=404, reason="x"), "gone")
        return self.messages[message_id]

    async def history(self, limit):
        for message in list(reversed(self.messages.values()))[:limit]:
            yield message


class FakeApi:
    def __init__(self):
        self.lobbies: list[dict] = []

    async def get_lobbies(self):
        return [dict(lobby) for lobby in self.lobbies]


@pytest.fixture
def mongo():
    return mongomock.MongoClient().db


@pytest.fixture
def repo(mongo):
    return LobbiesRepo(wrap(mongo.players), wrap(mongo.lobbies), wrap(mongo.roles_and_emojis), wrap(mongo.bot_state))


@pytest.fixture
def guild():
    return SimpleNamespace(roles=[SimpleNamespace(name=n, mention=f"<@&{n}>") for n in ("CrabHost", "AllHosts")])


@pytest.fixture
def channels(guild):
    return {"board": Channel(guild), "notify": Channel(guild)}


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def cog(api, repo, channels):
    by_id = {settings.lobby_channel_id: channels["board"], settings.notify_channel_id: channels["notify"]}
    bot = SimpleNamespace(get_channel=by_id.get, user=SimpleNamespace(id=BOT_ID))
    return LobbiesCog(bot, api, repo)
