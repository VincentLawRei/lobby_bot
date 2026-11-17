"""Фейки для тестов Twitch: локальный HTTP-сервер вместо Twitch API и каналы Discord в памяти."""

import discord
import mongomock
import pytest
from aiohttp import web

from lobby_bot.features.twitch import api as api_module
from lobby_bot.features.twitch.api import TwitchApi
from lobby_bot.features.twitch.cog import TwitchCog
from lobby_bot.features.twitch.repo import TwitchStateRepo
from lobby_bot.settings import settings
from tests.async_mongo import wrap


def make_stream(login: str, stream_id: str, language: str = "ru") -> dict:
    """Стрим в формате ответа Helix /streams."""
    return {
        "id": stream_id,
        "user_login": login,
        "user_name": login,
        "title": "title",
        "game_name": "Social Deduction Game",
        "viewer_count": 5,
        "language": language,
        "thumbnail_url": "https://img/{width}x{height}.jpg",
    }


class FakeTwitch:
    """Состояние фейкового Twitch: какие стримы идут, сколько раз выдавался токен."""

    def __init__(self) -> None:
        self.live: list[dict] = []
        self.token_requests = 0
        self.expire_token_once = False  # следующий запрос стримов ответит 401
        self.fail_status: int | None = None  # все запросы стримов отвечают этим статусом
        self.last_auth: str | None = None

    async def token(self, request: web.Request) -> web.Response:
        self.token_requests += 1
        return web.json_response({"access_token": f"token{self.token_requests}"})

    async def streams(self, request: web.Request) -> web.Response:
        self.last_auth = request.headers.get("Authorization")
        if self.expire_token_once:
            self.expire_token_once = False
            return web.Response(status=401)
        if self.fail_status:
            return web.Response(status=self.fail_status)
        login = request.query.get("user_login")
        data = [s for s in self.live if login is None or s["user_login"] == login]
        return web.json_response({"data": data})


@pytest.fixture
async def twitch(monkeypatch):
    """Запускает фейковый Twitch API на свободном порту и направляет на него TwitchApi."""
    fake = FakeTwitch()
    app = web.Application()
    app.router.add_post("/token", fake.token)
    app.router.add_get("/streams", fake.streams)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(api_module, "TOKEN_URL", f"http://127.0.0.1:{port}/token")
    monkeypatch.setattr(api_module, "STREAMS_URL", f"http://127.0.0.1:{port}/streams")
    yield fake
    await runner.cleanup()


@pytest.fixture
async def api(twitch):
    client = TwitchApi("client-id", "client-secret")
    await client.start()
    yield client
    await client.close()


class FakeMessage:
    def __init__(self, channel: "FakeChannel", message_id: int) -> None:
        self.channel = channel
        self.id = message_id

    async def edit(self, *, embed: discord.Embed) -> None:
        self.channel.edits.append(embed)


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "fake"


class FakeChannel:
    """Канал Discord: запоминает отправленные и отредактированные сообщения."""

    def __init__(self) -> None:
        self.sent: list[tuple[str | None, discord.Embed]] = []
        self.edits: list[discord.Embed] = []
        self.messages: dict[int, FakeMessage] = {}
        self.fail_send = False

    async def send(self, content: str | None = None, *, embed: discord.Embed) -> FakeMessage:
        if self.fail_send:
            raise discord.HTTPException(_Response(403), "forbidden")
        message = FakeMessage(self, 100 + len(self.sent))
        self.messages[message.id] = message
        self.sent.append((content, embed))
        return message

    async def fetch_message(self, message_id: int) -> FakeMessage:
        if message_id not in self.messages:
            raise discord.NotFound(_Response(404), "unknown message")
        return self.messages[message_id]


class FakeBot:
    def __init__(self, channels: dict[int, FakeChannel]) -> None:
        self.channels = channels

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.channels.get(channel_id)


@pytest.fixture
def channels() -> dict[str, FakeChannel]:
    return {"my_stream": FakeChannel(), "notify": FakeChannel(), "list": FakeChannel()}


@pytest.fixture
def repo() -> TwitchStateRepo:
    return TwitchStateRepo(wrap(mongomock.MongoClient().db.twitch))


@pytest.fixture
def cog(api, repo, channels) -> TwitchCog:
    bot = FakeBot(
        {
            settings.my_stream_channel_id_1: channels["my_stream"],
            settings.discord_channel_id_for_twitch_notifications: channels["notify"],
            settings.discord_channel_id_for_twitch_monitoring: channels["list"],
        }
    )
    return TwitchCog(bot, api, repo)
