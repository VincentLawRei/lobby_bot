"""Cog игровых серверов: команды запуска/остановки, модификаторы, принудительное завершение матча, панель и её задачи.

Перенесено из game/commands.py и game/elo_admin.py почти без изменений (эталоны: tests/features/servers/snapshots).
Меню и кнопки в views.py, правила и SSH в service.py / api.py. Cog держит репозитории: ServersRepo, ConsoleRepo
(снятие доступа к консоли), EloRepo и SeasonsRepo (бонусы предателей и сезон для ELO-кеша); views берут их у него
через клиент.

Старт: `cog_load` ставит задачу, которая после первого on_ready создаёт панель (если её нет) и запускает
фоновые задачи: обновление панели (5 мин), очистка резервов (1 мин), автоостановка через час (5 мин),
остановка сервера, если за 5 минут никто не зашёл (1 мин).
"""

import asyncio
import contextlib
import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from lobby_bot.features.console.repo import ConsoleRepo
from lobby_bot.features.elo.repo import EloRepo
from lobby_bot.features.seasons.repo import SeasonsRepo
from lobby_bot.features.seasons.service import seasons_repo
from lobby_bot.features.servers import service
from lobby_bot.features.servers.api import execute_ssh_command
from lobby_bot.features.servers.repo import ServersRepo
from lobby_bot.features.servers.service import (
    ADMINS,
    COMMANDS_CHANNELS,
    DEFAULT_MODIFIERS,
    DISABLED_SERVERS,
    HISTORY_CHANNEL_ID,
    MODIFIER_DESCRIPTIONS,
    PANEL_CHANNEL_ID,
    RESERVATION_LOG_CHANNEL_ID,
    SERVERS,
    SERVERS_UNAVAILABLE_MESSAGE,
    all_servers_disabled,
    clear_current_host,
    clear_reservation,
    get_current_host,
    get_panel_message_id,
    get_reservation,
    get_uptime_text,
    get_user_modifiers_raw,
    save_panel_message_id,
    save_user_modifiers,
    set_current_host,
)
from lobby_bot.features.servers.views import (
    RANKED_MODE_NOTE,
    ForceWinConfirmView,
    ForceWinServerChoiceView,
    ServerPanelView,
    ServerSettingsView,
    StopConfirmView,
    StopServerChoiceView,
    build_relicmod_embed,
    check_server_status,
    check_user_has_permission,
    get_user_preset,
    hosts_any_server,
    is_moderator,
    update_panel_embed,
)
from lobby_bot.settings import settings

logger = logging.getLogger(__name__)

SITE_URL = settings.site_url
COMMUNITY_PATCHER_URL = settings.community_patcher_url
COMMUNITY_RECONNECTOR_URL = settings.community_reconnector_url
DONATION_URL = settings.donation_url

BACKGROUND_TASKS = ("panel_update_task", "auto_stop_task", "reservation_cleanup_task", "failed_start_check_task")


class ServersCog(commands.Cog):
    def __init__(
        self, bot: commands.Bot, repo: ServersRepo, console_repo: ConsoleRepo, elo_repo: EloRepo, seasons: SeasonsRepo
    ) -> None:
        self.bot = bot
        self.repo = repo
        self.console_repo = console_repo
        self.elo_repo = elo_repo
        self.seasons = seasons
        self._startup_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        # Ссылку на задачу храним, иначе её может удалить сборщик мусора
        self._startup_task = asyncio.create_task(self._after_first_ready())

    async def cog_unload(self) -> None:
        if self._startup_task:
            self._startup_task.cancel()
        for name in BACKGROUND_TASKS:
            getattr(self, name).cancel()

    async def _after_first_ready(self) -> None:
        """Панели нужен кеш каналов, поэтому после on_ready. Первая итерация задач идёт сразу при start()."""
        await self.bot.wait_until_ready()
        try:
            await self.create_initial_panel()
            for name in BACKGROUND_TASKS:
                task = getattr(self, name)
                if not task.is_running():
                    task.start()
            logger.info("Фоновые задачи серверов запущены: %d", len(BACKGROUND_TASKS))
        except Exception:
            logger.exception("Ошибка при запуске панели и фоновых задач серверов")

    # ============ КОМАНДЫ ============
    @commands.command()
    async def gamepanel(self, ctx):
        """Создать панель управления серверами (только админы)"""
        if str(ctx.author.id) not in ADMINS:
            await ctx.send("❌ Только администраторы могут использовать эту команду!")
            return

        channel = self.bot.get_channel(PANEL_CHANNEL_ID)
        if not channel:
            await ctx.send("❌ Канал для панели не найден!")
            return

        # Получаем статус серверов
        description_parts = []

        for snum, scfg in SERVERS.items():
            sid = scfg["server_id"]
            sdata = await get_current_host(self.repo, sid)
            num_label = snum + 1
            region_label = scfg["region_name"]
            if sdata and sdata.get("host_name"):
                host_name = sdata.get("host_name")
                started_at = sdata.get("started_at")
                uptime = get_uptime_text(started_at)
                description_parts.append(f"**Сервер #{num_label} ({region_label}):**\n✅ Запущен\n👤 Хост: {host_name}")
                if uptime:
                    description_parts.append(f"⏱️ {uptime}")
            else:
                res = await get_reservation(self.repo, snum)
                if res:
                    description_parts.append(
                        f"**Сервер #{num_label} ({region_label}):** 🔒 Зарезервирован\n👤 {res['reserved_by_name']} до "
                        f"{res['reserved_until'].strftime('%H:%M')} UTC"
                    )
                else:
                    description_parts.append(f"**Сервер #{num_label} ({region_label}):** 🟢 Свободен")
            description_parts.append("")

        description_parts.append("\n*</srv:100000000000000002> для ЗАПУСКА сервера*")
        description_parts.append("")
        description_parts.append("*</stop:100000000000000003> для ОСТАНОВКИ своего сервера*")
        description_parts.append("*</crew_win:100000000000000004> - засчитать ПОБЕДУ мирным*")
        description_parts.append("*</traitors_win:100000000000000005> - засчитать ПОБЕДУ предателям*")
        description_parts.append("")
        description_parts.append("*</useful_things:100000000000000006> - получить доп. информацию по игре*")
        description_parts.append("")
        description_parts.append(RANKED_MODE_NOTE)

        embed = discord.Embed(
            title="🎮 Статус серверов Social Deduction Game",
            description="\n".join(description_parts),
            color=discord.Color.blue(),
        )

        view = ServerPanelView()
        msg = await channel.send(embed=embed, view=view)

        # Сохраняем ID сообщения
        await save_panel_message_id(self.repo, msg.id)

        await ctx.send(f"✅ Панель создана в канале <#{PANEL_CHANNEL_ID}>!")

    @app_commands.command(name="srv", description="Открыть меню настроек и запуска сервера")
    async def server_slash(self, interaction: discord.Interaction):
        """Slash-команда для запуска сервера"""
        # Проверка канала
        if interaction.channel_id not in COMMANDS_CHANNELS:
            await interaction.response.send_message(
                "❌ Эта команда доступна только в разрешенных каналах!", ephemeral=True
            )
            return

        if all_servers_disabled():
            await interaction.response.send_message(SERVERS_UNAVAILABLE_MESSAGE, ephemeral=True)
            return

        user_id = str(interaction.user.id)
        username = interaction.user.name

        if not check_user_has_permission(interaction):
            await interaction.response.send_message("❌ У вас нет прав для управления сервером!", ephemeral=True)
            return

        # Проверяем не запущен ли уже сервер этим пользователем (только для не-модераторов)
        if not is_moderator(interaction) and await hosts_any_server(self.repo, user_id):
            await interaction.response.send_message(
                "❌ У вас уже запущен сервер! Остановите его перед запуском нового.", ephemeral=True
            )
            return

        preset = await get_user_preset(self.repo, user_id, interaction)

        view = await ServerSettingsView.create(self.repo, user_id, username, preset)
        embed = view.get_initial_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="stop", description="Остановить сервер")
    async def stop_slash(self, interaction: discord.Interaction):
        """Slash-команда для остановки сервера"""
        # Проверка канала
        if interaction.channel_id not in COMMANDS_CHANNELS:
            await interaction.response.send_message(
                "❌ Эта команда доступна только в разрешенных каналах!", ephemeral=True
            )
            return

        user_id = str(interaction.user.id)

        if not check_user_has_permission(interaction):
            await interaction.response.send_message("❌ У вас нет прав для управления сервером!", ephemeral=True)
            return

        is_mod = is_moderator(interaction)

        # Собираем доступные серверы
        available_servers = []

        for server_num in SERVERS:
            running = await check_server_status(server_num)
            if running:
                server_data = await get_current_host(self.repo, SERVERS[server_num]["server_id"])
                if is_mod or (server_data and server_data.get("host_id") == user_id):
                    available_servers.append((server_num, server_data))

        if not available_servers:
            await interaction.response.send_message(
                "❌ Нет доступных серверов для остановки." if not is_mod else "❌ Нет запущенных серверов.",
                ephemeral=True,
            )
            return

        # Если один сервер - показываем подтверждение сразу
        if len(available_servers) == 1:
            server_num, server_data = available_servers[0]
            view = StopConfirmView(server_num, server_data, user_id)
            await interaction.response.send_message(view.get_confirmation_text(), view=view, ephemeral=True)
            return

        # Если несколько - показываем выбор
        view = StopServerChoiceView(available_servers, user_id)
        await interaction.response.send_message("Выберите сервер для остановки:", view=view, ephemeral=True)

    @app_commands.command(name="relicmod", description="Инструкция по установке RelicMod 1.1.4 и хостингу на Островах")
    async def relicmod_slash(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=build_relicmod_embed(), ephemeral=True)

    @app_commands.command(name="useful_things", description="Полезные ссылки и советы по Social Deduction Game")
    async def useful_things_slash(self, interaction: discord.Interaction):
        embed = discord.Embed(title="🔗 Полезные ссылки по Social Deduction Game", color=discord.Color.blurple())

        embed.add_field(
            name="Патчер-инджектор от COMMUNITY",
            value=(
                f"{COMMUNITY_PATCHER_URL}\n"
                "Нужна для того, чтобы функционировали разные клиентские патчи (реконнектор, фикс UI на 8+ игроков, "
                "лобби-патчи)"
            ),
            inline=False,
        )

        embed.add_field(
            name="Реконнектор от COMMUNITY",
            value=(
                f"{COMMUNITY_RECONNECTOR_URL}\n"
                "В большинстве случаев позволяет заходить в игры с войсом после крашей, пусть и не всегда. "
                "Необходима патчер."
            ),
            inline=False,
        )

        embed.add_field(
            name="Крашит игру при входе?",
            value=(
                "Вероятно, стоит одна из новых версий оверлея от Serega (он прекратил разработку, плюс Vule закрыл "
                "сервера). "
                "Можно поставить FreeTPшный или скачать старые версии с канала Неформала."
            ),
            inline=False,
        )

        embed.add_field(
            name="Поддержать сервера",
            value=(
                "На данный момент сервера оплачиваются самостоятельно, но если есть желание помочь "
                "с содержанием или добавлением новых серверов:\n"
                f"[DonationAlerts]({DONATION_URL})"
            ),
            inline=False,
        )

        embed.add_field(
            name="Новичок?", value=f"Советуем почитать гайды: [{SITE_URL.split('//', 1)[1]}]({SITE_URL})", inline=False
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="usermodifiers", description="Настроить пользовательские модификаторы сервера")
    @app_commands.describe(
        maxplayers="Макс. игроков (1–12)",
        daysbeforestorm="Дней до бури (2–7)",
        dayminutes="Минут в дне (5–16)",
        predatordamage="Урон волков, медведей (0.25–3)",
        weatherintensity="Интенсивность погоды (0.25–3)",
        staminarate="Скорость усталости (0.25–3)",
        fuelburnrate="Расход топлива (0.1–2)",
        traitors="Количество предателей (0–8)",
    )
    async def usermodifiers_slash(
        self,
        interaction: discord.Interaction,
        maxplayers: int | None = None,
        daysbeforestorm: int | None = None,
        dayminutes: int | None = None,
        predatordamage: float | None = None,
        weatherintensity: float | None = None,
        staminarate: float | None = None,
        fuelburnrate: float | None = None,
        traitors: int | None = None,
    ):
        """Slash-команда для настройки пользовательских модификаторов"""
        user_id = str(interaction.user.id)
        errors = []

        # Валидация
        if maxplayers is not None and not (1 <= maxplayers <= 12):
            errors.append("maxplayers: должно быть от 1 до 12")
        if daysbeforestorm is not None and not (2 <= daysbeforestorm <= 7):
            errors.append("daysbeforestorm: должно быть от 2 до 7")
        if dayminutes is not None and not (5 <= dayminutes <= 16):
            errors.append("dayminutes: должно быть от 5 до 16")
        if predatordamage is not None and not (0.25 <= predatordamage <= 3):
            errors.append("predatordamage: должно быть от 0.25 до 3")
        if weatherintensity is not None and not (0.25 <= weatherintensity <= 3):
            errors.append("weatherintensity: должно быть от 0.25 до 3")
        if staminarate is not None and not (0.25 <= staminarate <= 3):
            errors.append("staminarate: должно быть от 0.25 до 3")
        if fuelburnrate is not None and not (0.1 <= fuelburnrate <= 2):
            errors.append("fuelburnrate: должно быть от 0.1 до 2")
        if traitors is not None and not (0 <= traitors <= 8):
            errors.append("traitors: должно быть от 0 до 8")

        if errors:
            await interaction.response.send_message(
                "❌ Ошибки валидации:\n" + "\n".join(f"• {e}" for e in errors), ephemeral=True
            )
            return

        # Получаем текущие сохранённые модификаторы (или берём дефолты)
        existing_raw, _ = await get_user_modifiers_raw(self.repo, user_id)
        base = existing_raw or {}

        # Собираем итоговые модификаторы: если параметр передан — берём его, иначе из сохранённых или дефолт
        modifiers = {
            "maxplayers": maxplayers
            if maxplayers is not None
            else base.get("maxplayers", DEFAULT_MODIFIERS["maxplayers"]),
            "daysbeforestorm": daysbeforestorm
            if daysbeforestorm is not None
            else base.get("daysbeforestorm", DEFAULT_MODIFIERS["daysbeforestorm"]),
            "dayminutes": dayminutes
            if dayminutes is not None
            else base.get("dayminutes", DEFAULT_MODIFIERS["dayminutes"]),
            "predatordamage": predatordamage
            if predatordamage is not None
            else base.get("predatordamage", DEFAULT_MODIFIERS["predatordamage"]),
            "weatherintensity": weatherintensity
            if weatherintensity is not None
            else base.get("weatherintensity", DEFAULT_MODIFIERS["weatherintensity"]),
            "staminarate": staminarate
            if staminarate is not None
            else base.get("staminarate", DEFAULT_MODIFIERS["staminarate"]),
            "fuelburnrate": fuelburnrate
            if fuelburnrate is not None
            else base.get("fuelburnrate", DEFAULT_MODIFIERS["fuelburnrate"]),
            "traitors": traitors if traitors is not None else base.get("traitors", DEFAULT_MODIFIERS["traitors"]),
        }

        await save_user_modifiers(self.repo, user_id, modifiers)

        # Формируем отчёт
        lines = []
        for key, desc in MODIFIER_DESCRIPTIONS.items():
            val = modifiers[key]
            default_val = DEFAULT_MODIFIERS[key]
            changed = val != default_val
            marker = " ✏️" if changed else ""
            lines.append(f"**{desc}:** {val}{marker}")

        embed = discord.Embed(
            title="⚙️ Пользовательские модификаторы сохранены",
            description=(
                "✅ Значения сохранены. Включите модификаторы кнопкой **⚙️ Модификаторы** в меню запуска сервера.\n\n"
                + "\n".join(lines)
            ),
            color=discord.Color.green(),
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _force_win_slash(self, interaction: discord.Interaction, winner: int):
        """Общая логика для /crew_win и /traitors_win."""
        if interaction.channel_id not in COMMANDS_CHANNELS:
            await interaction.response.send_message(
                "❌ Эта команда доступна только в разрешённых каналах!", ephemeral=True
            )
            return

        if not check_user_has_permission(interaction):
            await interaction.response.send_message("❌ У вас нет прав для управления сервером!", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        is_mod = is_moderator(interaction)

        available_servers = []

        # Номер сервера и его данные берутся по одному ключу SERVERS: статус, хост и команда
        # завершения относятся к одному и тому же серверу.
        for server_num, scfg in sorted(SERVERS.items()):
            if not await check_server_status(server_num):
                continue
            server_data = await get_current_host(self.repo, scfg["server_id"])
            if is_mod or (server_data and server_data.get("host_id") == user_id):
                available_servers.append((server_num, server_data))

        if not available_servers:
            await interaction.response.send_message(
                "❌ Нет доступных серверов." if not is_mod else "❌ Нет запущенных серверов.", ephemeral=True
            )
            return

        if len(available_servers) == 1:
            server_num, server_data = available_servers[0]
            view = ForceWinConfirmView(server_num, server_data, user_id, winner)
            await interaction.response.send_message(view.get_confirmation_text(), view=view, ephemeral=True)
            return

        view = ForceWinServerChoiceView(available_servers, user_id, winner)
        winner_text = "победой мирных" if winner == 1 else "победой предателей"
        await interaction.response.send_message(
            f"Выберите сервер для завершения {winner_text}:", view=view, ephemeral=True
        )

    @app_commands.command(name="crew_win", description="Завершить матч победой мирных")
    async def crew_win_slash(self, interaction: discord.Interaction):
        await self._force_win_slash(interaction, winner=1)

    @app_commands.command(name="traitors_win", description="Завершить матч победой предателей")
    async def traitors_win_slash(self, interaction: discord.Interaction):
        await self._force_win_slash(interaction, winner=2)

    # ============ ПАНЕЛЬ И ФОНОВЫЕ ЗАДАЧИ ============
    async def create_initial_panel(self):
        """Создать панель при старте бота если её нет"""
        if await get_panel_message_id(self.repo):
            return  # Панель уже существует

        channel = self.bot.get_channel(PANEL_CHANNEL_ID)
        if not channel:
            logger.warning(f"⚠️ Канал для панели {PANEL_CHANNEL_ID} не найден!")
            return

        # Проверяем реальный статус серверов и синхронизируем с БД
        for snum, scfg in SERVERS.items():
            sid = scfg["server_id"]
            running = await check_server_status(snum)
            sdata = await get_current_host(self.repo, sid)
            if not running and sdata and sdata.get("host_name"):
                await clear_current_host(self.repo, self.console_repo, sid)
            elif running and (not sdata or not sdata.get("host_name")):
                await set_current_host(self.repo, sid, "unknown", "Неизвестно")

        description_parts = []

        for snum, scfg in SERVERS.items():
            sid = scfg["server_id"]
            sdata = await get_current_host(self.repo, sid)
            num_label = snum + 1
            region_label = scfg["region_name"]
            if snum in DISABLED_SERVERS:
                description_parts.append(f"**Сервер #{num_label} ({region_label}):** 🔴 Недоступен (отключён)")
            elif sdata and sdata.get("host_name"):
                host_name = sdata.get("host_name")
                started_at = sdata.get("started_at")
                uptime = get_uptime_text(started_at)
                description_parts.append(f"**Сервер #{num_label} ({region_label}):**\n✅ Запущен\n👤 Хост: {host_name}")
                if uptime:
                    description_parts.append(f"⏱️ {uptime}")
            else:
                description_parts.append(f"**Сервер #{num_label} ({region_label}):** 🟢 Свободен")
            description_parts.append("")

        description_parts.append("\n*</srv:100000000000000002> для запуска сервера*")
        description_parts.append("")
        description_parts.append("*</stop:100000000000000003> для остановки своего сервера*")
        description_parts.append("*</crew_win:100000000000000004> для того, чтобы дать победу мирным*")
        description_parts.append("*</traitors_win:100000000000000005> для того, чтобы дать победу предателям*")
        description_parts.append("")
        description_parts.append(
            "*</useful_things:100000000000000006> для того, чтобы получить полезные ссылки по SDG*"
        )
        description_parts.append("")
        description_parts.append(RANKED_MODE_NOTE)

        embed = discord.Embed(
            title="🎮 Статус серверов Social Deduction Game",
            description="\n".join(description_parts),
            color=discord.Color.blue(),
        )

        view = ServerPanelView()
        msg = await channel.send(embed=embed, view=view)

        # Сохраняем ID сообщения
        await save_panel_message_id(self.repo, msg.id)
        logger.info(f"✅ Панель создана с ID: {msg.id}")

    @tasks.loop(minutes=5)
    async def panel_update_task(self):
        try:
            await self._update_panel()
        except Exception:
            logger.exception("Ошибка задачи обновления панели")

    @tasks.loop(minutes=1)
    async def reservation_cleanup_task(self):
        try:
            await self._cleanup_reservations()
        except Exception:
            logger.exception("Ошибка задачи очистки резерваций")

    @tasks.loop(minutes=5)
    async def auto_stop_task(self):
        try:
            await self._auto_stop()
        except Exception:
            logger.exception("Ошибка задачи автоостановки")

    @tasks.loop(minutes=1)
    async def failed_start_check_task(self):
        try:
            await self._check_failed_starts()
        except Exception:
            logger.exception("Ошибка задачи проверки запуска")

    async def _update_panel(self):
        """Автоматическое обновление панели каждые 5 минут"""
        await update_panel_embed(self.bot)

    async def _cleanup_reservations(self):
        """Автоматическая очистка просроченных резерваций"""
        for snum in SERVERS:
            server_id = SERVERS[snum]["server_id"]
            doc = await get_current_host(self.repo, server_id)
            if not doc:
                continue
            reserved_until = doc.get("reserved_until")
            if not reserved_until:
                continue
            if service._now() > reserved_until:
                reserved_by_name = doc.get("reserved_by_name", "?")
                server_name = SERVERS[snum]["name"]
                region_name = SERVERS[snum]["region_name"]
                # Чистим
                await clear_reservation(self.repo, snum)
                # Уведомление
                channel = self.bot.get_channel(RESERVATION_LOG_CHANNEL_ID)
                if channel:
                    embed = discord.Embed(title="⏰ Резервация истекла", color=discord.Color.dark_grey())
                    embed.add_field(name="Сервер", value=f"{server_name} ({region_name})", inline=False)
                    embed.add_field(name="Был зарезервирован", value=reserved_by_name, inline=False)
                    with contextlib.suppress(Exception):
                        await channel.send(embed=embed)

    async def _auto_stop(self):
        """Автоматическая остановка серверов запущенных более 1 часа"""
        for snum in SERVERS:
            server_id = SERVERS[snum]["server_id"]
            doc = await get_current_host(self.repo, server_id)
            if not doc or not doc.get("host_name"):
                continue
            started_at = doc.get("started_at")
            if not started_at:
                continue
            if service._now() - started_at < timedelta(hours=1):
                continue

            # Сервер работает более 1 часа — останавливаем
            server_config = SERVERS[snum]
            screen_name = server_config["screen_name"]
            server_name = server_config["name"]
            region_name = server_config["region_name"]
            host_name = doc.get("host_name", "?")

            try:
                loop = asyncio.get_event_loop()
                stop_cmd = f"screen -S {screen_name} -X quit"
                await loop.run_in_executor(None, execute_ssh_command, stop_cmd, snum)
                await clear_current_host(self.repo, self.console_repo, server_id)
                logger.info(f"[AUTO-STOP] Сервер {server_name} остановлен автоматически (>1 часа)")
            except Exception as e:
                logger.exception(f"[AUTO-STOP] Ошибка остановки {server_name}: {e}")
                continue

            # Уведомление в лог-канал
            channel = self.bot.get_channel(HISTORY_CHANNEL_ID)
            if channel:
                embed = discord.Embed(title="⏰ Сервер остановлен автоматически", color=discord.Color.orange())
                embed.add_field(name="Сервер", value=f"{server_name} ({region_name})", inline=False)
                embed.add_field(name="Хост", value=host_name, inline=False)
                embed.add_field(name="Причина", value="Сервер работал более 1 часа", inline=False)
                with contextlib.suppress(Exception):
                    await channel.send(embed=embed)

            await update_panel_embed(self.bot)

    async def _check_failed_starts(self):
        """Останавливает сервер, если он запущен больше 5 минут,
        но в логах ещё нет строки 'Login request' (сервер не стартовал)"""
        for snum in SERVERS:
            server_id = SERVERS[snum]["server_id"]
            doc = await get_current_host(self.repo, server_id)
            if not doc or not doc.get("host_name"):
                continue
            started_at = doc.get("started_at")
            if not started_at:
                continue
            if service._now() - started_at < timedelta(minutes=5):
                continue

            server_config = SERVERS[snum]
            log_file = f"{server_config['folder']}/WindowsServer/Game/Saved/Logs/Game.log"
            check_cmd = f"grep -c 'Login request' {log_file} 2>/dev/null || echo 0"

            loop = asyncio.get_event_loop()
            try:
                stdout, _ = await loop.run_in_executor(None, execute_ssh_command, check_cmd, snum)
                count = int(stdout.strip() or "0")
            except Exception as e:
                logger.exception(f"[AUTO-STOP] Ошибка проверки логов {server_config['name']}: {e}")
                continue

            if count > 0:
                # Сервер стартовал нормально - больше не проверяем
                continue

            # Login request не найден спустя 5+ минуты - сервер не запустился
            screen_name = server_config["screen_name"]
            server_name = server_config["name"]
            region_name = server_config["region_name"]
            host_name = doc.get("host_name", "?")

            try:
                stop_cmd = f"screen -S {screen_name} -X quit"
                await loop.run_in_executor(None, execute_ssh_command, stop_cmd, snum)
                await clear_current_host(self.repo, self.console_repo, server_id)
                logger.info(f"[AUTO-STOP] Сервер {server_name} остановлен: нет 'Login request' в логах спустя 5 мин")
            except Exception as e:
                logger.exception(f"[AUTO-STOP] Ошибка остановки {server_name}: {e}")
                continue

            channel = self.bot.get_channel(HISTORY_CHANNEL_ID)
            if channel:
                embed = discord.Embed(title="⚠️ Сервер остановлен автоматически", color=discord.Color.red())
                embed.add_field(name="Сервер", value=f"{server_name} ({region_name})", inline=False)
                embed.add_field(name="Хост", value=host_name, inline=False)
                embed.add_field(
                    name="Причина", value="Сервер не запустился: за 5 минут никто не зашёл на сервер", inline=False
                )
                with contextlib.suppress(Exception):
                    await channel.send(embed=embed)

            await update_panel_embed(self.bot)


async def setup(bot: commands.Bot) -> None:
    # Persistent-панель: после рестарта кнопки старого сообщения панели продолжают работать
    bot.add_view(ServerPanelView())
    db = bot.db
    repo = ServersRepo(
        db.discord_hosts_notify,
        db.discord_hosts_presets,
        db.discord_panel,
        db.game_matches,
        db.player_ratings,
        db.history_player_stats,
    )
    elo_repo = EloRepo(
        db.player_ratings, db.history_player_stats, db.elo_history, db.matches, db.elo_processing, db.elo_overrides
    )
    await bot.add_cog(
        ServersCog(bot, repo, ConsoleRepo(db.console_access, db.player_locations), elo_repo, seasons_repo(db))
    )
