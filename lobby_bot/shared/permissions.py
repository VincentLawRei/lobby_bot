"""Проверка прав, общая для всех фич."""

import discord
from discord.ext import commands

from lobby_bot.settings import settings


def is_moderator(user: discord.abc.User) -> bool:
    """Модератор: роль MODERATOR_ROLE_ID на сервере или ID из ADMIN_IDS (владельцы бота)."""
    if user.id in settings.admin_ids:
        return True
    return any(role.id == settings.moderator_role_id for role in getattr(user, "roles", []))


class NotModerator(commands.CheckFailure):
    """Команда только для модераторов; LobbyBot.on_command_error отвечает пользователю."""


def moderator_check(ctx: commands.Context) -> bool:
    if not is_moderator(ctx.author):
        raise NotModerator()
    return True


def moderator_only():
    """Декоратор для отдельной !-команды. Для всех команд Cog: `cog_check = moderator_check`."""
    return commands.check(moderator_check)
