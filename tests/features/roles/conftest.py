"""Фейки Discord для тестов ролей: сервер, участники, роли, канал #roles и сообщение с реакциями."""

from types import SimpleNamespace

import discord
import mongomock
import pytest

from lobby_bot.features.roles.cog import RolesCog
from lobby_bot.features.roles.repo import RolesRepo
from tests.async_mongo import wrap

GUILD_ID = 1
ROLES_MESSAGE_ID = 500


class _Response:
    status = 404
    reason = "fake"


class FakeRole:
    def __init__(self, guild: "FakeGuild", name: str) -> None:
        self.guild, self.name = guild, name

    async def delete(self) -> None:
        self.guild.roles.remove(self)


class FakeMember:
    def __init__(self, user_id: int, bot: bool = False) -> None:
        self.id, self.bot, self.display_name = user_id, bot, f"user{user_id}"
        self.roles: list[FakeRole] = []

    async def add_roles(self, role: FakeRole) -> None:
        self.roles.append(role)

    async def remove_roles(self, role: FakeRole) -> None:
        self.roles.remove(role)


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id
        self.embed: discord.Embed | None = None
        self.reactions: list[SimpleNamespace] = []

    async def edit(self, *, embed: discord.Embed) -> None:
        self.embed = embed

    async def add_reaction(self, emoji: str) -> None:
        self.reactions.append(SimpleNamespace(emoji=emoji))

    async def clear_reaction(self, emoji: str) -> None:
        self.reactions = [r for r in self.reactions if r.emoji != emoji]


class FakeChannel:
    def __init__(self, name: str) -> None:
        self.name = name
        self.messages: dict[int, FakeMessage] = {}
        self.history_messages: list[FakeMessage] = []  # для channel.history(), новые первыми

    async def send(self, *, embed: discord.Embed) -> FakeMessage:
        message = FakeMessage(ROLES_MESSAGE_ID + len(self.messages))
        message.embed = embed
        self.messages[message.id] = message
        return message

    async def history(self, limit: int):
        for message in self.history_messages[:limit]:
            yield message

    async def fetch_message(self, message_id: int) -> FakeMessage:
        if message_id not in self.messages:
            raise discord.NotFound(_Response(), "unknown message")
        return self.messages[message_id]


class FakeGuild:
    def __init__(self) -> None:
        self.id = GUILD_ID
        self.roles: list[FakeRole] = []
        self.members: dict[int, FakeMember] = {}
        self.text_channels = [FakeChannel("roles")]

    def get_member(self, user_id: int) -> FakeMember | None:
        return self.members.get(user_id)

    async def fetch_member(self, user_id: int) -> FakeMember:
        raise discord.NotFound(_Response(), "unknown member")

    async def create_role(self, *, name: str) -> FakeRole:
        role = FakeRole(self, name)
        self.roles.append(role)
        return role

    def role_names(self) -> list[str]:
        return [r.name for r in self.roles]


class FakeBot:
    def __init__(self, guild: FakeGuild) -> None:
        self.guild = guild
        self.user = SimpleNamespace(name="bot")

    def get_all_channels(self):
        return list(self.guild.text_channels)

    def get_guild(self, guild_id: int) -> FakeGuild | None:
        return self.guild if guild_id == GUILD_ID else None


class FakeContext:
    """Контекст !-команды: сервер и ответы бота."""

    def __init__(self, guild: FakeGuild) -> None:
        self.guild = guild
        self.replies: list[str] = []

    async def send(self, text: str) -> None:
        self.replies.append(text)


def reaction(
    member: FakeMember | None, emoji: str, message_id: int = ROLES_MESSAGE_ID, guild_id: int | None = GUILD_ID
) -> SimpleNamespace:
    """Payload сырого события реакции (RawReactionActionEvent)."""
    return SimpleNamespace(
        guild_id=guild_id,
        message_id=message_id,
        user_id=member.id if member else 0,
        member=member,
        emoji=SimpleNamespace(name=emoji),
    )


@pytest.fixture
def repo() -> RolesRepo:
    client = mongomock.MongoClient()
    return RolesRepo(wrap(client.db.roles_and_emojis), wrap(client.db.bot_state))


@pytest.fixture
def guild() -> FakeGuild:
    return FakeGuild()


@pytest.fixture
def ctx(guild) -> FakeContext:
    return FakeContext(guild)


@pytest.fixture
def roles_channel(guild) -> FakeChannel:
    return guild.text_channels[0]


@pytest.fixture
def cog(repo, guild) -> RolesCog:
    return RolesCog(FakeBot(guild), repo)


@pytest.fixture
async def ready_cog(cog, repo, roles_channel, ctx) -> RolesCog:
    """Cog с ролями Tournament (🏆) и Host (🎮) и опубликованным сообщением выбора ролей."""
    await repo.add_role("Tournament", "🏆")
    await repo.add_role("Host", "🎮", steam_id="765")
    await cog.createmulti.callback(cog, ctx)
    return cog
