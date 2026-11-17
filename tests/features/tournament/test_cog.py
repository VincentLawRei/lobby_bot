from types import SimpleNamespace

import discord

from lobby_bot.features.tournament.cog import TournamentCog, participants_embed
from lobby_bot.settings import settings


def member(name, nick=None, bot=False):
    return SimpleNamespace(name=name, nick=nick, bot=bot)


def test_participants_sorted_by_display_name():
    embed = participants_embed([member("zed"), member("anna", nick="Боб"), member("Carl")])

    assert embed.description == "**Кол-во участников:** 3\n\n**Список:**\n- Carl\n- zed\n- Боб"


def test_no_participants():
    assert participants_embed([]).description.endswith("*Нет участников*")


class Channel:
    def __init__(self, guild, history):
        self.guild, self._history, self.sent = guild, history, []

    async def history(self, limit):
        for m in self._history[:limit]:
            yield m

    async def send(self, *, embed):
        self.sent.append(embed)


class OwnMessage:
    def __init__(self, author):
        self.author, self.embeds, self.edited = author, [discord.Embed()], None

    async def edit(self, *, embed):
        self.edited = embed


async def test_update_edits_own_message_and_skips_bots():
    me = SimpleNamespace(id=1)
    role = SimpleNamespace(name="Tournament", members=[member("a"), member("botty", bot=True)])
    own = OwnMessage(me)
    channel = Channel(SimpleNamespace(roles=[role]), [OwnMessage(SimpleNamespace(id=2)), own])
    bot = SimpleNamespace(user=me, get_channel=lambda i: channel if i == settings.tournament_track_channel_id else None)
    other = channel._history[0]

    await TournamentCog(bot).update()

    assert other.edited is None and channel.sent == []
    assert "**Кол-во участников:** 1" in own.edited.description


async def test_update_sends_new_message_when_none():
    me = SimpleNamespace(id=1)
    channel = Channel(SimpleNamespace(roles=[SimpleNamespace(name="Tournament", members=[])]), [])
    bot = SimpleNamespace(user=me, get_channel=lambda i: channel)

    await TournamentCog(bot).update()

    assert len(channel.sent) == 1
