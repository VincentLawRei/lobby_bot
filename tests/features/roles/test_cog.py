from types import SimpleNamespace

import discord
import pytest

from lobby_bot.shared.permissions import NotModerator
from tests.fakes import FakeContext, user

from .conftest import ROLES_MESSAGE_ID, FakeMember, FakeMessage, reaction


def _remove(member, emoji):
    payload = reaction(member, emoji)
    payload.member = None  # в событии снятия реакции Discord не присылает участника
    return payload


# ---------- Реакции ----------
async def test_reaction_gives_role_and_creates_it_if_missing(ready_cog, guild):
    member = FakeMember(10)

    await ready_cog.on_raw_reaction_add(reaction(member, "🏆"))

    assert [r.name for r in member.roles] == ["Tournament"]
    assert "Tournament" in guild.role_names()


async def test_removing_reaction_takes_role_away(ready_cog, guild):
    member = FakeMember(10)
    guild.members[10] = member
    await ready_cog.on_raw_reaction_add(reaction(member, "🏆"))

    await ready_cog.on_raw_reaction_remove(_remove(member, "🏆"))

    assert member.roles == []


async def test_reaction_on_other_message_ignored(ready_cog):
    member = FakeMember(10)

    await ready_cog.on_raw_reaction_add(reaction(member, "🏆", message_id=999))

    assert member.roles == []


async def test_reactions_ignored_until_roles_message_known(cog, repo):
    """Раньше без сохранённого ID роли выдавались за реакцию на любом сообщении сервера."""
    await repo.add_role("Tournament", "🏆")
    member = FakeMember(10)

    await cog.on_raw_reaction_add(reaction(member, "🏆", message_id=777))

    assert member.roles == []


async def test_bots_and_unknown_emoji_ignored(ready_cog):
    bot_member, member = FakeMember(1, bot=True), FakeMember(10)

    await ready_cog.on_raw_reaction_add(reaction(bot_member, "🏆"))
    await ready_cog.on_raw_reaction_add(reaction(member, "🍕"))

    assert bot_member.roles == [] and member.roles == []


async def test_reaction_outside_guild_ignored(ready_cog):
    await ready_cog.on_raw_reaction_add(reaction(FakeMember(10), "🏆", guild_id=None))  # не падает


async def test_remove_reaction_of_member_who_left(ready_cog):
    await ready_cog.on_raw_reaction_remove(_remove(FakeMember(10), "🏆"))  # не падает


# ---------- Команды ----------
async def test_createmulti_posts_message_with_reactions(ready_cog, repo, roles_channel):
    message = roles_channel.messages[ROLES_MESSAGE_ID]

    assert message.embed.title == "Выбор роли"
    assert "участник турнира" in message.embed.description
    assert [r.emoji for r in message.reactions] == ["🏆", "🎮"]
    assert await repo.get_roles_message_id() == ROLES_MESSAGE_ID


async def test_addhost_adds_role_and_updates_message(ready_cog, repo, guild, ctx, roles_channel):
    await ready_cog.addhost.callback(ready_cog, ctx, "NewHost", "🦀", "111")

    message = roles_channel.messages[ROLES_MESSAGE_ID]
    assert (await repo.get_roles())["NewHost"] == "🦀"
    assert "NewHost" in guild.role_names()
    assert "NewHost" in message.embed.description
    assert message.reactions[-1].emoji == "🦀"
    assert ctx.replies[-1] == "Добавлен новый хост: NewHost (🦀, steam_id: 111)"


async def test_addhost_rejects_duplicate(ready_cog, ctx):
    await ready_cog.addhost.callback(ready_cog, ctx, "Host", "🎮")

    assert ctx.replies == ["Такая роль уже есть!"]


async def test_removehost_removes_everywhere(ready_cog, repo, guild, ctx, roles_channel):
    await guild.create_role(name="Host")

    await ready_cog.removehost.callback(ready_cog, ctx, "Host")

    message = roles_channel.messages[ROLES_MESSAGE_ID]
    assert "Host" not in await repo.get_roles()
    assert "Host" not in guild.role_names()
    assert "Host" not in message.embed.description
    assert [r.emoji for r in message.reactions] == ["🏆"]


async def test_removehost_unknown_role(ready_cog, ctx):
    await ready_cog.removehost.callback(ready_cog, ctx, "Nope")

    assert ctx.replies == ["Роли Nope не существует!"]


async def test_setrolesmsg_checks_message_exists(ready_cog, repo, ctx):
    await ready_cog.setrolesmsg.callback(ready_cog, ctx, 999)

    assert ctx.replies == ["Сообщение с таким id не найдено в канале #roles."]
    assert await repo.get_roles_message_id() == ROLES_MESSAGE_ID


async def test_showrolesmsg(ready_cog, cog, ctx):
    await ready_cog.showrolesmsg.callback(ready_cog, ctx)

    assert ctx.replies == [f"Сейчас используется сообщение с id: {ROLES_MESSAGE_ID}"]


# ---------- Поиск сообщения с ролями при старте ----------
async def test_on_ready_uses_saved_message_id(cog, repo):
    await repo.set_roles_message_id(42)

    await cog.on_ready()

    assert cog.roles_message_id == 42


async def test_on_ready_finds_roles_message_in_channel(cog, repo, roles_channel):
    old = FakeMessage(41)
    old.author, old.embeds = cog.bot.user, [discord.Embed(title="Выбор роли")]
    other = FakeMessage(43)
    other.author, other.embeds = SimpleNamespace(), [discord.Embed(title="Выбор роли")]  # чужое сообщение
    roles_channel.history_messages = [other, old]

    await cog.on_ready()

    assert cog.roles_message_id == 41
    assert await repo.get_roles_message_id() == 41


async def test_commands_only_for_moderators(cog):
    with pytest.raises(NotModerator):
        cog.cog_check(FakeContext(author=user(10)))
