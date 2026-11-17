"""Обработчик ошибок !-команд в LobbyBot."""

import logging

from discord.ext import commands

from lobby_bot.app import bot
from lobby_bot.shared.permissions import NotModerator
from tests.fakes import FakeContext


async def test_not_moderator_gets_reply():
    ctx = FakeContext()
    await bot.on_command_error(ctx, NotModerator())
    assert ctx.replies == ["❌ Недостаточно прав."]


async def test_unknown_command_ignored(caplog):
    ctx = FakeContext()
    await bot.on_command_error(ctx, commands.CommandNotFound())
    assert ctx.replies == [] and not caplog.records


async def test_other_errors_logged(caplog):
    ctx = FakeContext()
    with caplog.at_level(logging.ERROR):
        await bot.on_command_error(ctx, commands.CommandError("boom"))
    assert ctx.replies == []
    assert "Ошибка в команде" in caplog.text
