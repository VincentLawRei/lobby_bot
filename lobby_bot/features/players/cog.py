"""Команды модераторов по игрокам: !search, !nick, !twitch."""

from discord.ext import commands

from lobby_bot.shared.permissions import moderator_check

from .repo import PlayersRepo

MESSAGE_LIMIT = 1800  # Discord ограничивает сообщение 2000 символами


class PlayersCog(commands.Cog):
    def __init__(self, bot: commands.Bot, repo: PlayersRepo) -> None:
        self.bot = bot
        self.repo = repo

    def cog_check(self, ctx: commands.Context) -> bool:
        return moderator_check(ctx)

    @commands.command()
    async def search(self, ctx: commands.Context, *, nickname_part: str) -> None:
        """
        Поиск игроков по части ника.
        Показывает game_uid, nickname, other_id, дату.
        """
        results = await self.repo.search_by_nickname(nickname_part)
        if not results:
            await ctx.send("Ничего не найдено.")
            return

        text = ""
        for doc in results:
            date = doc.get("date")
            text += (
                f"**Ник:** {doc.get('nickname', '—')}\n"
                f"**Social Deduction Game ID:** `{doc.get('game_uid', '—')}`\n"
                f"**SteamID (other_id):** `{doc.get('other_id', '—')}`\n"
                f"**Последняя дата:** {date.strftime('%Y-%m-%d %H:%M') if date else '—'}\n"
                "--------------------------\n"
            )
            if len(text) > MESSAGE_LIMIT:
                await ctx.send(text)
                text = ""
        if text:
            await ctx.send(text)

    @commands.command()
    async def nick(self, ctx: commands.Context, identifier: str, *, changednickname: str) -> None:
        """
        Изменить поле changed_nickname для всех записей игрока.
        Можно использовать либо game_uid, либо steam_id (other_id).
        Пример: !nick 76561198000000000 НовыйНик
        """
        await self._set_player_field(ctx, identifier, "changed_nickname", changednickname, "Новый ник", "ник")

    @commands.command()
    async def twitch(self, ctx: commands.Context, identifier: str, *, twitch_username: str) -> None:
        """
        Добавить поле twitch для всех записей игрока.
        Можно использовать либо game_uid, либо steam_id (other_id).
        Пример: !twitch 76561198000000000 twitch_nick
        """
        await self._set_player_field(ctx, identifier, "twitch", twitch_username, "Twitch", "Twitch")

    async def _set_player_field(
        self, ctx: commands.Context, identifier: str, field: str, value: str, label: str, unchanged_label: str
    ) -> None:
        steam_id = await self.repo.find_steam_id(identifier)
        if steam_id is None:
            await ctx.send(f"❌ Игрок с ID `{identifier}` не найден.")
            return
        modified = await self.repo.set_for_all_records(steam_id, field, value)
        if modified > 0:
            await ctx.send(f"✅ Обновлено **{modified}** записей для steam_id `{steam_id}`.\n{label}: **{value}**")
        else:
            await ctx.send(f"⚠️ Записи найдены, но {unchanged_label} уже был `{value}`.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PlayersCog(bot, PlayersRepo(bot.db.players)))
