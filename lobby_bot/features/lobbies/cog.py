"""Мониторинг лобби Social Deduction Game (LOBBY_MONITOR_ENABLED), раз в минуту:
- доска всех лобби в LOBBY_CHANNEL_ID (одно сообщение, редактируется);
- уведомления о лобби хостов в NOTIFY_CHANNEL_ID с пингом роли хоста и AllHosts;
- игроки из лобби сохраняются в `players`, снапшоты заполненных лобби в `lobbies` (для графов).
"""

import logging
from datetime import UTC, datetime, timedelta

import discord
import pytz
from discord.ext import commands, tasks

from lobby_bot.settings import secret_value, settings

from .api import EosLobbyApi
from .repo import LobbiesRepo

logger = logging.getLogger(__name__)

MOSCOW = pytz.timezone("Europe/Moscow")
PINNED_HOST = "Antony"  # лобби этого хоста всегда первые на доске
NEW_NOTIFICATION_INTERVAL = timedelta(minutes=45)  # чаще новое уведомление (с пингом) не создаётся
ALL_HOSTS_ROLE = "AllHosts"
SNAPSHOT_MIN_PLAYERS = 8
NOTIFY_TIME_FORMAT = "%Y-%m-%d %H:%M:%S%z"  # формат времени в bot_state (совместим со старыми записями)
MESSAGE_LIMIT = 2000


class LobbiesCog(commands.Cog):
    def __init__(self, bot: commands.Bot, api: EosLobbyApi, repo: LobbiesRepo) -> None:
        self.bot = bot
        self.api = api
        self.repo = repo

    async def cog_load(self) -> None:
        if settings.lobby_monitor_enabled:
            await self.api.start()
            self.monitor_task.start()

    async def cog_unload(self) -> None:
        self.monitor_task.cancel()
        await self.api.close()

    @tasks.loop(seconds=60)
    async def monitor_task(self) -> None:
        try:
            await self.update()
        except Exception:
            logger.exception("Ошибка мониторинга лобби")

    @monitor_task.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def update(self) -> None:
        board_channel = self.bot.get_channel(settings.lobby_channel_id)
        notify_channel = self.bot.get_channel(settings.notify_channel_id)
        if board_channel is None or notify_channel is None:
            return

        try:
            lobbies = await self.api.get_lobbies()
        except Exception:
            logger.exception("Ошибка API лобби")
            lobbies = []
        lobbies.sort(key=lambda x: (PINNED_HOST not in x["lobby_host_name"].lower(), -x["total_players"]))

        now = datetime.now(UTC)
        db_now = now.replace(tzinfo=None)  # в Mongo даты хранятся наивными в UTC
        hosts = await self.repo.hosts_by_steam_id()
        notified_hosts: set[str] = set()
        blocks = []
        for lobby in lobbies:
            steam_id, game_uid = lobby["leader_id"][0], lobby["leader_id"][1]
            host_name, other_name = await self._host_names(lobby)
            await self.repo.upsert_player(host_name, game_uid, steam_id, db_now)

            if steam_id in hosts and steam_id not in notified_hosts:
                await self._notify_host(hosts[steam_id], lobby, notify_channel, now)
                notified_hosts.add(steam_id)

            for player in lobby["lobby_players"]:
                await self.repo.upsert_player(player["name"], player["id"], player["diff_id"], db_now)
            if lobby["total_players"] >= SNAPSHOT_MIN_PLAYERS and not lobby["is_server_listening"]:
                await self.repo.save_snapshot(lobby, db_now)

            names, missing = await self.repo.public_player_names(lobby["public_players_ids"])
            blocks.append(lobby_block(lobby, host_name, other_name, names, missing, now))

        await self._remove_stale_notifications(notify_channel, notified_hosts)
        await self._publish_board(board_channel, board_text(blocks, now))

    async def _host_names(self, lobby: dict) -> tuple[str, str]:
        """(ник хоста для доски, игровой ник, если модератор задал другой; иначе "")."""
        changed = await self.repo.changed_nickname(lobby["leader_id"][0])
        if changed is None:
            return lobby["lobby_host_name"], ""
        other = lobby["lobby_host_name"] if changed != lobby["lobby_host_name"] else ""
        return changed, other

    async def _publish_board(self, channel, text: str) -> None:
        async for message in channel.history(limit=5):
            if message.author.id == self.bot.user.id:
                await message.edit(content=text)
                return
        await channel.send(text)

    # ---------- Уведомления хостов ----------
    async def _notify_host(self, role_name: str, lobby: dict, channel, now: datetime) -> None:
        """Лобби ждёт игроков: новое сообщение с пингом не чаще раза в 45 минут, иначе правка старого.
        Лобби уже в игре: правка сообщения с пометкой «Уже запустились»."""
        _, created = await self.repo.get_notify_state(role_name)
        if lobby["is_server_listening"]:
            create_new = needs_new_notification(created, now)
            await self._update_notification(role_name, lobby, channel, now, create_new, started=False)
        else:
            await self._update_notification(role_name, lobby, channel, now, create_new=False, started=True)

    async def _update_notification(
        self, role_name: str, lobby: dict, channel, now: datetime, create_new: bool, started: bool
    ) -> None:
        host_role = discord.utils.get(channel.guild.roles, name=role_name)
        all_hosts_role = discord.utils.get(channel.guild.roles, name=ALL_HOSTS_ROLE)
        if host_role is None or all_hosts_role is None:
            return
        role_doc = await self.repo.host_role(role_name)
        text = notification_text(
            host_role.mention, all_hosts_role.mention, lobby, role_doc.get("description") if role_doc else None, started
        )

        old_message_id, _ = await self.repo.get_notify_state(role_name)
        if old_message_id and not create_new:
            try:
                message = await channel.fetch_message(old_message_id)
                await message.edit(content=text)
                return
            except discord.HTTPException:
                pass  # сообщение удалили: отправим новое

        message = await channel.send(text)
        await self.repo.set_notify_state(role_name, message.id, now.strftime(NOTIFY_TIME_FORMAT))
        if old_message_id and old_message_id != message.id:
            await _delete_message(channel, old_message_id)

    async def _remove_stale_notifications(self, channel, notified_hosts: set[str]) -> None:
        """Удалить уведомления хостов, чьих лобби больше нет."""
        for role_name in await self.repo.notified_roles():
            role_doc = await self.repo.host_role(role_name)
            steam_id = role_doc.get("steam_id") if role_doc else None
            if not steam_id or steam_id in notified_hosts:
                continue
            message_id, _ = await self.repo.get_notify_state(role_name)
            if message_id:
                await _delete_message(channel, message_id)
                await self.repo.set_notify_state(role_name, None, None)


async def _delete_message(channel, message_id: int) -> None:
    try:
        message = await channel.fetch_message(message_id)
        await message.delete()
    except discord.HTTPException:
        pass  # уже удалено


# ---------- Текст ----------
def needs_new_notification(created: str | None, now: datetime) -> bool:
    if not created:
        return True
    try:
        created_at = datetime.strptime(created, NOTIFY_TIME_FORMAT)
    except ValueError:
        return True
    return now - created_at >= NEW_NOTIFICATION_INTERVAL


def notification_text(
    host_mention: str, all_hosts_mention: str, lobby: dict, description: str | None, started: bool
) -> str:
    free = lobby["open_public_players"]
    text = f"{host_mention} +{free} \n\n{all_hosts_mention}\n"
    if description and description.strip():
        text += f"||\n**Описание:** {description}||"
    if started:
        text += "\nУже запустились в игру: да"
    return text


def lobby_block(lobby: dict, host_name: str, other_name: str, names: list[str], missing: int, now: datetime) -> str:
    occupied = f"{lobby['total_players']} / {lobby['total_players'] + lobby['open_public_players']}"
    created_at = (now.astimezone(MOSCOW) - timedelta(minutes=lobby["time_from_creation"])).strftime("%H:%M")
    waiting = ":ballot_box_with_check:" if lobby["is_server_listening"] else ":x:"
    host = f"**{other_name}** ({host_name})" if other_name else f"**{host_name}**"
    text = (
        "----------------------\n"
        f"👤 {host} [{lobby['language']}] ({occupied})\n"
        "----------------------\n"
        f"В лобби: {waiting}\n"
        f"Создано: {lobby['time_from_creation']} мин назад ({created_at})\n"
        "Игроки в лобби:\n`"
    )
    if names:
        text += ", ".join(names) + ", " + ", ".join("???" for _ in range(missing))
    else:
        text += "Ничего не найдено"
    return text.rstrip(", ") + "`\n\n"


def board_text(blocks: list[str], now: datetime) -> str:
    text = "# **Social Deduction Game Lobbies (EU+RU) at this moment:** \n\n" + "".join(blocks)
    if not blocks:
        text += "Не найдено ни одного лобби... :<\n\n"
    text += f"_Обновлено: {now.astimezone(MOSCOW).strftime('%d-%m-%Y %H:%M:%S')} (GMT +3)_\n\n"
    return text[:MESSAGE_LIMIT]


async def setup(bot: commands.Bot) -> None:
    api = EosLobbyApi(
        secret_value(settings.game_header_authorization),
        {
            "external_auth_token": secret_value(settings.external_auth_token),
            "deployment_id": settings.deployment_id or "",
            "nonce": settings.nonce or "",
            "display_name": settings.display_name or "",
        },
    )
    await bot.add_cog(LobbiesCog(bot, api, LobbiesRepo(bot.db.players, bot.db.lobbies, bot.db.roles, bot.db.state)))
