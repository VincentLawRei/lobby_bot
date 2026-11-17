from types import SimpleNamespace

import discord
import mongomock
import pytest

from lobby_bot.features.dm_log.cog import DmLogCog
from tests.async_mongo import wrap


class FakeDM(discord.DMChannel):
    """DMChannel без подключения к Discord: только send."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


class FakeGuildChannel:
    async def send(self, text: str) -> None:
        raise AssertionError("в каналы сервера бот не отвечает")


BOT_USER = SimpleNamespace(id=1)


@pytest.fixture
def col():
    return mongomock.MongoClient().db.dm_logs


@pytest.fixture
def cog(col):
    return DmLogCog(SimpleNamespace(user=BOT_USER), wrap(col))


class Author:
    id = 10

    def __str__(self) -> str:
        return "user#0001"


async def test_dm_saved_and_confirmed(cog, col):
    channel = FakeDM()
    await cog.on_message(SimpleNamespace(author=Author(), channel=channel, content="привет"))

    doc = col.find_one()
    assert (doc["user_id"], doc["username"], doc["message"]) == (10, "user#0001", "привет")
    assert channel.sent == ["Спасибо, твоё сообщение сохранено!"]


async def test_guild_messages_ignored(cog, col):
    await cog.on_message(SimpleNamespace(author=Author(), channel=FakeGuildChannel(), content="x"))
    assert col.count_documents({}) == 0


async def test_own_messages_ignored(cog, col):
    await cog.on_message(SimpleNamespace(author=BOT_USER, channel=FakeDM(), content="x"))
    assert col.count_documents({}) == 0
