"""ELO в Discord.

EloCog — задачи: раз в 5 минут расчёт матчей из очереди, раз в 5 минут снятие штрафов с истёкшим сроком,
раз в 6 часов срез ELO за инактив с постом в лог-канал. Посчитанные матчи уходят событием
`elo_matches_processed` со списком (match_uuid, season): итоги в канал отправляет match_results,
таблицу обновляет leaderboard.

EloAdminCog — команды модераторов: /elo_adjust, /elo_adjust_group, /elo_adjust_group_certain, /link_twink,
/unlink_twink, /elo_revert_match, /elo_penalty, /elo_penalty_remove (только роль модератора, без ADMIN_IDS).
"""

import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from lobby_bot.features.seasons.repo import SeasonsRepo
from lobby_bot.features.seasons.service import current_season, season_or_first, seasons_repo
from lobby_bot.settings import settings

from . import service
from .repo import EloRepo

logger = logging.getLogger(__name__)


def build_decay_embed(players: list[dict]) -> discord.Embed:
    embed = discord.Embed(
        title="⚠️ Снижение ELO за неактивность",
        description="Следующие игроки не сыграли ни одного матча за последнюю неделю, "
        "в связи с чем их рейтинг был снижен:",
        color=discord.Color.red(),
    )
    lines = "".join(
        f"• **{p['name']}**: {p['old_elo']} ➔ `{p['new_elo']}` ELO (`-{service.DECAY_ELO_PER_DAY}`)\n" for p in players
    )
    embed.add_field(name="Список игроков:", value=lines, inline=False)
    embed.set_footer(text="Играйте минимум 1 матч в неделю, чтобы не терять рейтинг.")
    embed.timestamp = discord.utils.utcnow()
    return embed


class EloCog(commands.Cog):
    def __init__(
        self, bot: commands.Bot, repo: EloRepo, seasons: SeasonsRepo, log_channel_id: int, elo_log_channel_id: int = 0
    ) -> None:
        self.bot = bot
        self.repo = repo
        self.seasons = seasons
        self.log_channel_id = log_channel_id  # пост о decay
        self.elo_log_channel_id = elo_log_channel_id  # лог модераторских правок и автоснятия штрафов
        self._indexes_ready = False

    async def cog_load(self) -> None:
        self.elo_processing_task.start()
        self.daily_elo_decay_task.start()
        self.penalty_expiry_task.start()

    async def cog_unload(self) -> None:
        self.elo_processing_task.cancel()
        self.daily_elo_decay_task.cancel()
        self.penalty_expiry_task.cancel()

    # ---------- Очередь матчей ----------
    @tasks.loop(seconds=300)
    async def elo_processing_task(self) -> None:
        try:
            season, pending = await self._prepare()
        except Exception:
            logger.exception("Ошибка чтения очереди ELO")
            return

        processed = []
        for match in pending:
            match_uuid = match['match_uuid']
            try:
                if not await service.process_queued_match(self.repo, match_uuid, season):
                    continue
            except Exception:
                logger.exception("Ошибка расчёта ELO матча %s", match_uuid)
                continue
            processed.append((match_uuid, season))

        if processed:
            self.bot.dispatch("elo_matches_processed", processed)

    async def _prepare(self) -> tuple[int, list[dict]]:
        if not self._indexes_ready:  # один раз за запуск, но не в cog_load: недоступная база не должна мешать старту
            await self.repo.ensure_indexes()
            self._indexes_ready = True
        return await current_season(self.seasons), await self.repo.pending_matches()

    # ---------- Decay ----------
    @tasks.loop(hours=6)
    async def daily_elo_decay_task(self) -> None:
        try:
            decayed = await service.process_elo_decay(self.repo, await current_season(self.seasons))
            if not decayed:
                return
            logger.info("[DECAY] Рейтинг срезан у %d игроков", len(decayed))
            channel = self.bot.get_channel(self.log_channel_id)
            if channel:
                await channel.send(embed=build_decay_embed(decayed))
            else:
                logger.error("[DECAY] Не найден канал %s", self.log_channel_id)
        except Exception:
            logger.exception("[DECAY] Ошибка среза ELO за инактив")

    # ---------- Снятие штрафов по сроку ----------
    @tasks.loop(minutes=5)
    async def penalty_expiry_task(self) -> None:
        try:
            season = await season_or_first(self.seasons)
            expired = await service.expire_penalties(self.repo, season)
        except Exception:
            logger.exception("[PENALTY] Ошибка снятия штрафов")
            return
        channel = self.bot.get_channel(self.elo_log_channel_id)
        if not channel:
            return
        for player in expired:
            embed = discord.Embed(title="⏰ Штраф ELO снят автоматически", color=discord.Color.blurple())
            embed.add_field(name="Игрок", value=f"**{player.get('current_name', player['steam_id'])}**", inline=False)
            embed.add_field(name="Причина", value="Истёк срок штрафа", inline=False)
            try:
                await channel.send(embed=embed)
            except Exception:
                logger.exception("[PENALTY] Не удалось отправить в лог")

    @elo_processing_task.before_loop
    @daily_elo_decay_task.before_loop
    @penalty_expiry_task.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()


# ============ Команды модераторов ============
def _has_moderator_role(interaction: discord.Interaction, role_id: int) -> bool:
    """Как раньше в этих командах: только роль модератора на сервере (без ADMIN_IDS)."""
    member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
    return bool(member) and any(role.id == role_id for role in member.roles)


def _player_choices(players: list[dict]) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(
            name=f"{p.get('current_name', 'Unknown')} — {p.get('steam_id', '')}", value=p.get('steam_id', '')
        )
        for p in players
    ]


def _change_line(c: service.EloChange) -> str:
    sign = "+" if c.change > 0 else ""
    return f"**{c.name}**: {c.elo_before} → **{c.elo_after}** ({sign}{c.change})"


class EloAdminCog(commands.Cog):
    def __init__(
        self, bot: commands.Bot, repo: EloRepo, seasons: SeasonsRepo, elo_log_channel_id: int, moderator_role_id: int
    ) -> None:
        self.bot = bot
        self.repo = repo
        self.seasons = seasons
        self.elo_log_channel_id = elo_log_channel_id
        self.moderator_role_id = moderator_role_id

    def _is_mod(self, interaction: discord.Interaction) -> bool:
        return _has_moderator_role(interaction, self.moderator_role_id)

    async def _log(self, interaction: discord.Interaction, embed: discord.Embed) -> None:
        channel = interaction.client.get_channel(self.elo_log_channel_id)
        if channel:
            await channel.send(embed=embed)

    async def _search(self, current: str, **kwargs) -> list[dict]:
        season = await season_or_first(self.seasons)
        return await self.repo.search_players(season, current, **kwargs)

    async def player_autocomplete(self, interaction: discord.Interaction, current: str):
        return _player_choices(await self._search(current))

    async def _adjust_many(
        self, interaction: discord.Interaction, pairs: list[tuple[str, int]], reason: str
    ) -> list[str]:
        season = await season_or_first(self.seasons)
        lines = []
        for steam_id, points in pairs:
            change = await service.adjust_elo(
                self.repo, steam_id, points, reason, str(interaction.user.id), interaction.user.display_name, season
            )
            lines.append(_change_line(change) if change else f"❌ `{steam_id}` — не найден")
        return lines

    # ---------- /elo_adjust ----------
    @app_commands.command(name="elo_adjust", description="Изменить ELO игрока вручную")
    @app_commands.describe(
        steam_id="Steam ID игрока",
        points="Количество очков (положительное или отрицательное)",
        reason="Причина изменения",
    )
    async def elo_adjust(self, interaction: discord.Interaction, steam_id: str, points: int, reason: str):
        if not self._is_mod(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        season = await season_or_first(self.seasons)
        change = await service.adjust_elo(
            self.repo, steam_id, points, reason, str(interaction.user.id), interaction.user.display_name, season
        )
        if not change:
            await interaction.response.send_message(
                f"❌ Игрок со Steam ID `{steam_id}` не найден в сезоне {season}.", ephemeral=True
            )
            return
        sign = "+" if points > 0 else ""
        embed = discord.Embed(
            title=f"📊 ELO {'повышено' if points > 0 else 'понижено'}",
            color=discord.Color.green() if points > 0 else discord.Color.red(),
        )
        embed.add_field(name="Игрок", value=f"**{change.name}**", inline=False)
        embed.add_field(
            name="Изменение", value=f"{change.elo_before} → **{change.elo_after}** ({sign}{points})", inline=True
        )
        embed.add_field(name="Причина", value=reason, inline=False)
        embed.set_footer(text=f"Изменено: {interaction.user.display_name}")
        await self._log(interaction, embed)
        await interaction.response.send_message(
            f"✅ ELO игрока **{change.name}** изменено: {change.elo_before} → **{change.elo_after}** ({sign}{points})",
            ephemeral=True,
        )

    @elo_adjust.autocomplete('steam_id')
    async def _elo_adjust_ac(self, interaction: discord.Interaction, current: str):
        return await self.player_autocomplete(interaction, current)

    # ---------- /link_twink ----------
    @app_commands.command(name="link_twink", description="Привязать твинк к основному аккаунту")
    @app_commands.describe(twink="Твинк-аккаунт", main="Основной аккаунт")
    async def link_twink(self, interaction: discord.Interaction, twink: str, main: str):
        if not self._is_mod(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        if twink == main:
            await interaction.response.send_message("❌ Твинк и основа не могут быть одним аккаунтом.", ephemeral=True)
            return
        season = await current_season(self.seasons)
        twink_player = await self.repo.rating(twink, season)
        main_player = await self.repo.rating(main, season)
        if not twink_player:
            await interaction.response.send_message(
                f"❌ Твинк-аккаунт `{twink}` не найден в сезоне {season}.", ephemeral=True
            )
            return
        if not main_player:
            await interaction.response.send_message(
                f"❌ Основной аккаунт `{main}` не найден в сезоне {season}.", ephemeral=True
            )
            return
        await service.link_twink(self.repo, twink, main, season)
        await interaction.response.send_message(
            f"✅ Твинк **{twink_player.get('current_name', twink)}** (`{twink}`) привязан к основному "
            f"**{main_player.get('current_name', main)}** (`{main}`).",
            ephemeral=True,
        )

    @link_twink.autocomplete('twink')
    @link_twink.autocomplete('main')
    async def _link_twink_ac(self, interaction: discord.Interaction, current: str):
        return await self.player_autocomplete(interaction, current)

    # ---------- /unlink_twink ----------
    @app_commands.command(name="unlink_twink", description="Снять привязку твинка к основному аккаунту")
    @app_commands.describe(twink="Твинк-аккаунт")
    async def unlink_twink(self, interaction: discord.Interaction, twink: str):
        if not self._is_mod(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        season = await current_season(self.seasons)
        twink_player = await self.repo.rating(twink, season)
        if not twink_player:
            await interaction.response.send_message(
                f"❌ Аккаунт `{twink}` не найден в сезоне {season}.", ephemeral=True
            )
            return
        if not twink_player.get('is_twink'):
            await interaction.response.send_message(
                f"❌ **{twink_player.get('current_name', twink)}** (`{twink}`) не привязан как твинк.", ephemeral=True
            )
            return
        await service.unlink_twink(self.repo, twink, season)
        await interaction.response.send_message(
            f"✅ Привязка снята: **{twink_player.get('current_name', twink)}** (`{twink}`) больше не твинк "
            f"`{twink_player.get('linked_to')}`.",
            ephemeral=True,
        )

    @unlink_twink.autocomplete('twink')
    async def _unlink_twink_ac(self, interaction: discord.Interaction, current: str):
        return await self.player_autocomplete(interaction, current)

    # ---------- /elo_adjust_group ----------
    @app_commands.command(name="elo_adjust_group", description="Изменить ELO нескольким игрокам сразу")
    @app_commands.describe(
        player1="Игрок 1",
        player2="Игрок 2",
        player3="Игрок 3",
        player4="Игрок 4",
        player5="Игрок 5",
        player6="Игрок 6",
        points="Количество очков (положительное или отрицательное)",
        reason="Причина изменения",
    )
    async def elo_adjust_group(
        self,
        interaction: discord.Interaction,
        points: int,
        reason: str,
        player1: str,
        player2: str | None = None,
        player3: str | None = None,
        player4: str | None = None,
        player5: str | None = None,
        player6: str | None = None,
    ):
        if not self._is_mod(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        steam_ids = [p for p in [player1, player2, player3, player4, player5, player6] if p]
        lines = await self._adjust_many(interaction, [(sid, points) for sid in steam_ids], reason)
        sign = "+" if points > 0 else ""
        embed = discord.Embed(
            title=f"📊 ELO {'повышено' if points > 0 else 'понижено'} ({sign}{points})",
            color=discord.Color.green() if points > 0 else discord.Color.red(),
        )
        embed.add_field(name="Игроки", value="\n".join(lines), inline=False)
        embed.add_field(name="Причина", value=reason, inline=False)
        embed.set_footer(text=f"Изменено: {interaction.user.display_name}")
        await self._log(interaction, embed)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @elo_adjust_group.autocomplete('player1')
    @elo_adjust_group.autocomplete('player2')
    @elo_adjust_group.autocomplete('player3')
    @elo_adjust_group.autocomplete('player4')
    @elo_adjust_group.autocomplete('player5')
    @elo_adjust_group.autocomplete('player6')
    async def _group_ac(self, interaction: discord.Interaction, current: str):
        return await self.player_autocomplete(interaction, current)

    # ---------- /elo_adjust_group_certain ----------
    @app_commands.command(
        name="elo_adjust_group_certain", description="Изменить ELO нескольким игрокам с индивидуальными значениями"
    )
    @app_commands.describe(
        reason="Причина изменения",
        player1="Игрок 1",
        points1="Изменение ELO для игрока 1",
        player2="Игрок 2",
        points2="Изменение ELO для игрока 2",
        player3="Игрок 3",
        points3="Изменение ELO для игрока 3",
        player4="Игрок 4",
        points4="Изменение ELO для игрока 4",
        player5="Игрок 5",
        points5="Изменение ELO для игрока 5",
        player6="Игрок 6",
        points6="Изменение ELO для игрока 6",
    )
    async def elo_adjust_group_certain(
        self,
        interaction: discord.Interaction,
        reason: str,
        player1: str,
        points1: int,
        player2: str | None = None,
        points2: int | None = None,
        player3: str | None = None,
        points3: int | None = None,
        player4: str | None = None,
        points4: int | None = None,
        player5: str | None = None,
        points5: int | None = None,
        player6: str | None = None,
        points6: int | None = None,
    ):
        if not self._is_mod(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        pairs = [
            (sid, pts)
            for sid, pts in [
                (player1, points1),
                (player2, points2),
                (player3, points3),
                (player4, points4),
                (player5, points5),
                (player6, points6),
            ]
            if sid and pts is not None
        ]
        if not pairs:
            await interaction.response.send_message(
                "❌ Укажите хотя бы одного игрока с изменением ELO.", ephemeral=True
            )
            return
        lines = await self._adjust_many(interaction, pairs, reason)
        embed = discord.Embed(title="📊 ELO изменено (индивидуально)", color=discord.Color.blurple())
        embed.add_field(name="Игроки", value="\n".join(lines), inline=False)
        embed.add_field(name="Причина", value=reason, inline=False)
        embed.set_footer(text=f"Изменено: {interaction.user.display_name}")
        await self._log(interaction, embed)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @elo_adjust_group_certain.autocomplete('player1')
    @elo_adjust_group_certain.autocomplete('player2')
    @elo_adjust_group_certain.autocomplete('player3')
    @elo_adjust_group_certain.autocomplete('player4')
    @elo_adjust_group_certain.autocomplete('player5')
    @elo_adjust_group_certain.autocomplete('player6')
    async def _certain_ac(self, interaction: discord.Interaction, current: str):
        return await self.player_autocomplete(interaction, current)

    # ---------- /elo_revert_match ----------
    @app_commands.command(name="elo_revert_match", description="Откатить или отменить откат ELO по UUID матча")
    @app_commands.describe(match_uuid="UUID матча (можно частичный, например 9FF94EB9)")
    async def elo_revert_match(self, interaction: discord.Interaction, match_uuid: str):
        if not self._is_mod(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        season = await season_or_first(self.seasons)
        result = await service.revert_match(
            self.repo, match_uuid, season, str(interaction.user.id), interaction.user.display_name
        )
        if not result:
            await interaction.followup.send(f"❌ Матч `{match_uuid}` не найден в истории ELO.", ephemeral=True)
            return
        embed = discord.Embed(
            title="↩️ Отмена отмены рейтинга" if result.is_undo else "🔄 Получение-понижение ELO рейтинга отменено",
            color=discord.Color.green() if result.is_undo else discord.Color.orange(),
        )
        embed.add_field(name="Матч", value=f"`{result.match_uuid}`", inline=False)
        embed.add_field(name="Игроки", value="\n".join(result.lines), inline=False)
        embed.set_footer(text=f"Изменено: {interaction.user.display_name}")
        await self._log(interaction, embed)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @elo_revert_match.autocomplete('match_uuid')
    async def _revert_ac(self, interaction: discord.Interaction, current: str):
        season = await season_or_first(self.seasons)
        matches = await service.recent_matches_for_autocomplete(self.repo, season, current)
        return [
            app_commands.Choice(name=f"{uuid[:8]}... — {dt.strftime('%d.%m %H:%M') if dt else '?'}", value=uuid)
            for uuid, dt in matches
        ]

    # ---------- /elo_penalty ----------
    @app_commands.command(name="elo_penalty", description="Установить штраф/бонус ELO для игрока")
    @app_commands.describe(
        steam_id="Игрок (поиск по нику или Steam ID)",
        win_reduction="Снижение получаемого ELO при победе, % (по умолчанию 30)",
        loss_increase="Увеличение потерь ELO при поражении, % (по умолчанию 30)",
        reason="Причина штрафа",
        duration_minutes="На сколько минут (необязательно, если не указано — бессрочно)",
    )
    async def elo_penalty(
        self,
        interaction: discord.Interaction,
        steam_id: str,
        reason: str,
        win_reduction: int = 30,
        loss_increase: int = 30,
        duration_minutes: int | None = None,
    ):
        if not self._is_mod(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        season = await season_or_first(self.seasons)
        if not (0 <= win_reduction <= 100):
            await interaction.response.send_message("❌ win_reduction должен быть от 0 до 100.", ephemeral=True)
            return
        if not (0 <= loss_increase <= 500):
            await interaction.response.send_message("❌ loss_increase должен быть от 0 до 500.", ephemeral=True)
            return
        until = None
        if duration_minutes is not None:
            if duration_minutes <= 0:
                await interaction.response.send_message("❌ Количество минут должно быть больше 0.", ephemeral=True)
                return
            until = service._now() + timedelta(minutes=duration_minutes)

        player = await self.repo.rating(steam_id, season)
        if not player:
            await interaction.response.send_message(f"❌ Игрок `{steam_id}` не найден.", ephemeral=True)
            return
        win_multiplier = round(1.0 - win_reduction / 100, 4)
        loss_multiplier = round(1.0 + loss_increase / 100, 4)
        await service.set_penalty(
            self.repo,
            steam_id,
            win_multiplier,
            loss_multiplier,
            reason,
            until,
            str(interaction.user.id),
            interaction.user.display_name,
            season,
        )

        name = player.get("current_name", steam_id)
        until_str = until.strftime("%d.%m.%Y") if until else "бессрочно"
        embed = discord.Embed(title="⚠️ Штраф ELO установлен", color=discord.Color.orange())
        embed.add_field(name="Игрок", value=f"**{name}**", inline=False)
        embed.add_field(name="Победа", value=f"-{win_reduction}% к получаемому ELO (×{win_multiplier})", inline=True)
        embed.add_field(name="Поражение", value=f"+{loss_increase}% к потерям ELO (×{loss_multiplier})", inline=True)
        embed.add_field(name="Причина", value=reason, inline=False)
        embed.add_field(name="Действует до", value=until_str, inline=True)
        embed.set_footer(text=f"Установил: {interaction.user.display_name}")
        await self._log(interaction, embed)
        await interaction.response.send_message(
            f"✅ Штраф установлен для **{name}**: победа -{win_reduction}%, поражение +{loss_increase}%, до "
            f"{until_str}",
            ephemeral=True,
        )

    @elo_penalty.autocomplete('steam_id')
    async def _penalty_ac(self, interaction: discord.Interaction, current: str):
        try:
            players = await self._search(current, with_nickname=True)
            return [
                app_commands.Choice(
                    name=f"{p.get('changed_nickname') or p.get('current_name', 'Unknown')}"
                    f"{' ⚠️' if p.get('is_penalized') else ''} — {p.get('steam_id', '')}",
                    value=p.get('steam_id', ''),
                )
                for p in players
            ]
        except Exception:
            logger.exception("[AUTOCOMPLETE ERROR] /elo_penalty")
            return []

    # ---------- /elo_penalty_remove ----------
    @app_commands.command(name="elo_penalty_remove", description="Снять штраф ELO с игрока")
    @app_commands.describe(steam_id="Игрок (поиск по нику или Steam ID)")
    async def elo_penalty_remove(self, interaction: discord.Interaction, steam_id: str):
        if not self._is_mod(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return
        season = await season_or_first(self.seasons)
        player = await self.repo.rating(steam_id, season)
        if not player:
            await interaction.response.send_message(f"❌ Игрок `{steam_id}` не найден.", ephemeral=True)
            return
        if not player.get("is_penalized"):
            await interaction.response.send_message("ℹ️ У игрока нет активного штрафа.", ephemeral=True)
            return
        await service.remove_penalty(self.repo, steam_id, season)
        name = player.get("current_name", steam_id)
        embed = discord.Embed(title="✅ Штраф ELO снят", color=discord.Color.green())
        embed.add_field(name="Игрок", value=f"**{name}**", inline=False)
        embed.set_footer(text=f"Снял: {interaction.user.display_name}")
        await self._log(interaction, embed)
        await interaction.response.send_message(f"✅ Штраф снят с **{name}**", ephemeral=True)

    @elo_penalty_remove.autocomplete('steam_id')
    async def _penalty_remove_ac(self, interaction: discord.Interaction, current: str):
        players = await self._search(current, with_nickname=True, only_penalized=True)
        choices = []
        for p in players:
            name = p.get("changed_nickname") or p.get("current_name", "Unknown")
            reason = p.get("penalty_reason", "")
            label = f"{name} — {reason[:20] + '...' if len(reason) > 20 else reason}" if reason else name
            choices.append(app_commands.Choice(name=label, value=p.get("steam_id", "")))
        return choices


async def setup(bot: commands.Bot) -> None:
    db = bot.db
    repo = EloRepo(
        db.player_ratings, db.history_player_stats, db.elo_history, db.matches, db.elo_processing, db.elo_overrides
    )
    seasons = seasons_repo(db)
    await bot.add_cog(EloCog(bot, repo, seasons, settings.elo_calculator_log_channel_id, settings.elo_log_channel_id))
    await bot.add_cog(EloAdminCog(bot, repo, seasons, settings.elo_log_channel_id, settings.moderator_role_id))
