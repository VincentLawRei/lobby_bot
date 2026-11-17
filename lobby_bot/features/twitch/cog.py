"""Twitch-оповещения (общий функционал, не привязан к Social Deduction Game).

- my_stream_task: стрим владельца бота, сообщение с пингом роли (включено всегда).
- monitor_task: русскоязычные стримеры категории: уведомление о новых + список в одном сообщении
  (включается флагом TWITCH_MONITOR_ENABLED).
"""

import logging

import aiohttp
import discord
from discord.ext import commands, tasks

from lobby_bot.settings import secret_value, settings

from .api import TwitchApi
from .repo import TwitchStateRepo

logger = logging.getLogger(__name__)


class TwitchCog(commands.Cog):
    def __init__(self, bot: commands.Bot, api: TwitchApi, repo: TwitchStateRepo) -> None:
        self.bot = bot
        self.api = api
        self.repo = repo
        # Каналы для уведомления о стриме владельца: имя -> ID (имя хранится в БД как ключ состояния)
        self.my_stream_targets = {
            "channel_1": settings.my_stream_channel_id_1,
            # "channel_2": settings.my_stream_channel_id_2,
        }
        self.live_streamers: set[str] = set()  # кто стримил на прошлой проверке (для monitor_task)

    async def cog_load(self) -> None:
        if not settings.twitch_client_id or not settings.twitch_client_secret:
            logger.warning("TWITCH_CLIENT_ID/TWITCH_CLIENT_SECRET не заданы: Twitch-оповещения выключены")
            return
        await self.api.start()
        self.my_stream_task.start()
        if settings.twitch_monitor_enabled:
            self.monitor_task.start()

    async def cog_unload(self) -> None:
        self.my_stream_task.cancel()
        self.monitor_task.cancel()
        await self.api.close()

    # ---------- Стрим владельца ----------
    @tasks.loop(seconds=60)
    async def my_stream_task(self) -> None:
        try:
            streams = await self.api.get_streams(user_login=settings.my_twitch_login)
            if not streams:
                return  # оффлайн
            await self._notify_my_stream(streams[0])
        except aiohttp.ClientError as e:
            logger.warning("Twitch API недоступен: %s", e)
        except Exception:
            # Без перехвата любая ошибка навсегда остановила бы задачу
            logger.exception("Ошибка проверки стрима владельца")

    async def _notify_my_stream(self, stream: dict) -> None:
        embed = my_stream_embed(stream)
        for target, channel_id in self.my_stream_targets.items():
            if await self.repo.get_last_notified_stream(target) == stream["id"]:
                continue  # об этом стриме здесь уже уведомили

            channel = self.bot.get_channel(channel_id)
            if channel is None:
                logger.warning("⚠️ Канал %s (%s) не найден", target, channel_id)
                continue
            try:
                await channel.send(content=f"<@&{settings.my_stream_role_id}>", embed=embed)
            except discord.HTTPException:
                logger.exception("⚠️ Не удалось отправить уведомление в %s", target)
                continue
            # Запоминаем стрим только после успешной отправки
            await self.repo.set_last_notified_stream(target, stream["id"])

    # ---------- Стримеры категории ----------
    @tasks.loop(seconds=60)
    async def monitor_task(self) -> None:
        try:
            streams = await self.api.get_streams(game_id=settings.twitch_category_id, first=10)
            streams = [s for s in streams if s.get("language") == "ru"]
            await self._notify_new_streamers(streams)
            await self._update_stream_list(streams)
            self.live_streamers = {s["user_name"] for s in streams}
        except aiohttp.ClientError as e:
            logger.warning("Twitch API недоступен: %s", e)
        except Exception:
            logger.exception("Ошибка мониторинга стримов категории")

    async def _notify_new_streamers(self, streams: list[dict]) -> None:
        channel = self.bot.get_channel(settings.discord_channel_id_for_twitch_notifications)
        if channel is None:
            logger.warning("Канал Twitch-уведомлений не найден")
            return
        role_mention = f"<@&{settings.twitch_streams_role_id}>"
        for stream in streams:
            if stream["user_name"] not in self.live_streamers:
                await channel.send(content=role_mention, embed=new_streamer_embed(stream))

    async def _update_stream_list(self, streams: list[dict]) -> None:
        channel = self.bot.get_channel(settings.discord_channel_id_for_twitch_monitoring)
        if channel is None:
            logger.warning("Канал списка стримеров не найден")
            return
        embed = stream_list_embed(streams)
        message_id = await self.repo.get_stream_list_message_id()
        if message_id:
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(embed=embed)
                return
            except discord.NotFound:
                pass  # сообщение удалили: отправим новое
        message = await channel.send(embed=embed)
        await self.repo.set_stream_list_message_id(message.id)

    @my_stream_task.before_loop
    @monitor_task.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()


# ---------- Embed'ы ----------
def my_stream_embed(stream: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"🔴 {stream['user_name']} сейчас стримит!",
        description=f"🎮 {stream['title']}\n🕹 {stream['game_name']}",
        url=f"https://twitch.tv/{stream['user_name']}",
        color=discord.Color.purple(),
    )
    # Twitch отдаёт один и тот же URL превью для всех стримов канала, а Discord кеширует картинки по URL.
    # Добавляем id стрима, чтобы у каждого стрима была своя свежая картинка.
    thumbnail = stream["thumbnail_url"].format(width=640, height=360)
    embed.set_image(url=f"{thumbnail}?stream={stream['id']}")
    return embed


def new_streamer_embed(stream: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"{stream['user_name']} начал стримить!",
        description=f"🎮 {stream['title']}\n👀 {stream['viewer_count']} зрителей",
        url=f"https://twitch.tv/{stream['user_name']}",
        color=discord.Color.purple(),
    )
    embed.set_thumbnail(url=stream["thumbnail_url"].format(width=640, height=360))
    return embed


def stream_list_embed(streams: list[dict]) -> discord.Embed:
    if not streams:
        return discord.Embed(
            title="Сейчас никто не стримит",
            description="Правило #1957 - всегда должен быть стрим по Social Deduction Game.",
            color=discord.Color.blue(),
        )
    embed = discord.Embed(title="Список активных стримеров:", color=discord.Color.blue())
    for s in streams:
        embed.add_field(
            name=f"{s['user_name']} ({s['viewer_count']} зрителей)",
            value=f"🎮 {s['title']}\n[twitch.tv/{s['user_name']}](https://twitch.tv/{s['user_name']})",
            inline=False,
        )
    return embed


async def setup(bot: commands.Bot) -> None:
    """Точка входа расширения: bot.load_extension("lobby_bot.features.twitch.cog")."""
    api = TwitchApi(settings.twitch_client_id or "", secret_value(settings.twitch_client_secret))
    await bot.add_cog(TwitchCog(bot, api, TwitchStateRepo(bot.db.twitch)))
