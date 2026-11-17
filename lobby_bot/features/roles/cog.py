"""Роли по реакциям: сообщение «Выбор роли» в #roles, реакция = роль (подписка на уведомления).

Команды (префикс !, только модераторы): createmulti, addhost, removehost, setrolesmsg, setdesc, showrolesmsg.
"""

import logging

import discord
from discord.ext import commands

from lobby_bot.shared.permissions import moderator_check

from .repo import RolesRepo

logger = logging.getLogger(__name__)

ROLES_CHANNEL_NAME = "roles"
ROLES_EMBED_TITLE = "Выбор роли"

# Пояснения к особым ролям в сообщении с выбором ролей
ROLE_HINTS = {
    "AllHosts": "уведомления обо всех хостах",
    "TournamentWomen": "возможная участница ЖЕНСКОГО турнира",
    "TwitchNotificationBot": "уведомления для Twitch стримов в категории Social Deduction Game",
    "NewPlayers": "для того, чтобы пинговать новичков и тех, кто хочет поиграть в новичковых лобби",
    "Tournament": "участник турнира",
}
INTRO = (
    "Получаем роль - получаем уведомления по роли.\n\n"
    "Важно: если вдруг я удалил сообщение (пока тесты), а вы хотите отписаться - нажать на эмодзи и снова отжать\n\n"
    "Пока что: максимум 15 хостов, будет время или необходимость - добавлю больше слотов\n\n"
)


class RolesCog(commands.Cog):
    def __init__(self, bot: commands.Bot, repo: RolesRepo) -> None:
        self.bot = bot
        self.repo = repo
        self.roles_message_id: int | None = None

    def cog_check(self, ctx: commands.Context) -> bool:
        # Только для команд; реакции (listeners) доступны всем
        return moderator_check(ctx)

    # ---------- Сообщение с ролями ----------
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self.roles_message_id = await self.repo.get_roles_message_id()
        if self.roles_message_id:
            return
        # ID не сохранён: ищем своё сообщение «Выбор роли» в #roles
        channel = discord.utils.get(self.bot.get_all_channels(), name=ROLES_CHANNEL_NAME)
        if channel is None:
            logger.warning("Канал #roles не найден.")
            return
        async for message in channel.history(limit=100):
            if (
                message.author == self.bot.user
                and message.embeds
                and ROLES_EMBED_TITLE in (message.embeds[0].title or "")
            ):
                await self._remember_roles_message(message.id)
                return
        logger.warning("Сообщение с ролями не найдено. Используйте !createmulti или !setrolesmsg.")

    async def _remember_roles_message(self, message_id: int) -> None:
        self.roles_message_id = message_id
        await self.repo.set_roles_message_id(message_id)

    # ---------- Реакции ----------
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if not self._is_roles_message(payload):
            return
        member = payload.member
        if member is None or member.bot:
            return
        guild = self.bot.get_guild(payload.guild_id)
        for role_name in await self._roles_for_emoji(payload.emoji):
            role = discord.utils.get(guild.roles, name=role_name)
            if role is None:
                role = await guild.create_role(name=role_name)
            await member.add_roles(role)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if not self._is_roles_message(payload):
            return
        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.NotFound:
                return  # пользователь уже вышел с сервера
        if member.bot:
            return
        for role_name in await self._roles_for_emoji(payload.emoji):
            role = discord.utils.get(guild.roles, name=role_name)
            if role:
                await member.remove_roles(role)
                logger.info("Роль %s снята с %s", role_name, member.display_name)

    def _is_roles_message(self, payload: discord.RawReactionActionEvent) -> bool:
        return (
            payload.guild_id is not None
            and self.roles_message_id is not None
            and payload.message_id == self.roles_message_id
        )

    async def _roles_for_emoji(self, emoji: discord.PartialEmoji) -> list[str]:
        # Роли читаются из БД при каждой реакции: правки в Mongo применяются без рестарта
        return [role for role, role_emoji in (await self.repo.get_roles()).items() if role_emoji == emoji.name]

    # ---------- Команды ----------
    @commands.command()
    async def createmulti(self, ctx: commands.Context) -> None:
        """Создать embed-сообщение с выбором ролей"""
        channel = self._roles_channel(ctx)
        if channel is None:
            await ctx.send("Канал #roles не найден.")
            return
        roles = await self.repo.get_roles()
        message = await channel.send(embed=roles_embed(roles))
        for emoji in roles.values():
            await message.add_reaction(emoji)
        await self._remember_roles_message(message.id)

    @commands.command()
    async def addhost(self, ctx: commands.Context, role_name: str, emoji: str, steam_id: str | None = None) -> None:
        """Добавить нового хоста (роль, эмодзи, [необязательно] Steam ID)"""
        if role_name in await self.repo.get_roles():
            await ctx.send("Такая роль уже есть!")
            return
        await self.repo.add_role(role_name, emoji, steam_id)

        if discord.utils.get(ctx.guild.roles, name=role_name) is None:
            try:
                await ctx.guild.create_role(name=role_name)
                await ctx.send(f"Создана роль {role_name} в Discord.")
            except Exception as e:
                await ctx.send(f"Ошибка создания роли {role_name} в Discord: {e}")

        channel = self._roles_channel(ctx)
        if channel is None:
            await ctx.send("Канал #roles не найден.")
            return
        if not self.roles_message_id:
            await ctx.send("Нет активного сообщения с ролями.")
            return
        message = await channel.fetch_message(self.roles_message_id)
        await message.edit(embed=roles_embed(await self.repo.get_roles()))
        await message.add_reaction(emoji)
        if steam_id:
            await ctx.send(f"Добавлен новый хост: {role_name} ({emoji}, steam_id: {steam_id})")
        else:
            await ctx.send(f"Добавлена новая роль: {role_name} ({emoji})")

    @commands.command()
    async def setrolesmsg(self, ctx: commands.Context, msg_id: int) -> None:
        """Вручную указать id сообщения с ролями"""
        channel = self._roles_channel(ctx)
        if channel is None:
            await ctx.send("Канал #roles не найден.")
            return
        try:
            await channel.fetch_message(msg_id)
        except discord.NotFound:
            await ctx.send("Сообщение с таким id не найдено в канале #roles.")
            return
        await self._remember_roles_message(msg_id)
        await ctx.send(f"Id сообщения с ролями ({msg_id}) успешно установлен!")

    @commands.command()
    async def removehost(self, ctx: commands.Context, role_name: str) -> None:
        """Удалить роль и эмодзи из списка и из Discord"""
        emoji = (await self.repo.get_roles()).get(role_name)
        if emoji is None:
            await ctx.send(f"Роли {role_name} не существует!")
            return
        await self.repo.remove_role(role_name)

        role = discord.utils.get(ctx.guild.roles, name=role_name)
        if role:
            try:
                await role.delete()
                await ctx.send(f"Роль {role_name} удалена из Discord.")
            except Exception as e:
                await ctx.send(f"Не удалось удалить роль из Discord: {e}")

        channel = self._roles_channel(ctx)
        if channel is None or not self.roles_message_id:
            await ctx.send("Не удалось найти embed-сообщение с ролями.")
            return
        message = await channel.fetch_message(self.roles_message_id)
        await message.edit(embed=roles_embed(await self.repo.get_roles()))
        await ctx.send(f"Роль и эмодзи для {role_name} удалены из embed.")

        found = False
        for reaction in message.reactions:
            # Юникод-эмодзи сравниваем строкой, кастомные по имени
            if str(reaction.emoji) == emoji or getattr(reaction.emoji, "name", None) == emoji:
                try:
                    await message.clear_reaction(reaction.emoji)
                    found = True
                    await ctx.send(f"Эмодзи {emoji} удалён из реакций.")
                except Exception as e:
                    await ctx.send(f"Ошибка при удалении реакции {emoji}: {e}")
        if not found:
            await ctx.send(f"Не найдено совпадение для эмодзи {emoji} среди реакций.")

    @commands.command()
    async def setdesc(self, ctx: commands.Context, role_name: str, *, description: str) -> None:
        if await self.repo.set_description(role_name, description):
            await ctx.send(f"Описание для роли **{role_name}** успешно обновлено!")
        else:
            await ctx.send(f"Роль **{role_name}** не найдена в базе.")

    @commands.command()
    async def showrolesmsg(self, ctx: commands.Context) -> None:
        """Показать id текущего сообщения с ролями"""
        if self.roles_message_id:
            await ctx.send(f"Сейчас используется сообщение с id: {self.roles_message_id}")
        else:
            await ctx.send("Id сообщения с ролями не установлен.")

    @staticmethod
    def _roles_channel(ctx: commands.Context) -> discord.TextChannel | None:
        return discord.utils.get(ctx.guild.text_channels, name=ROLES_CHANNEL_NAME)


def roles_embed(roles: dict[str, str]) -> discord.Embed:
    lines = []
    for role, emoji in roles.items():
        hint = ROLE_HINTS.get(role)
        lines.append(f"**{emoji} — {role}** *({hint})*\n\n" if hint else f"{emoji} — {role}\n")
    return discord.Embed(title=ROLES_EMBED_TITLE, description=INTRO + "".join(lines), color=discord.Color.blue())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RolesCog(bot, RolesRepo(bot.db.roles, bot.db.state)))
