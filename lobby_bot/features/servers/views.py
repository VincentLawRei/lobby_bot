"""Discord-часть игровых серверов: панель, меню запуска, остановка, резервы, принудительное завершение матча.

Код перенесён из game/views.py и game/elo_admin.py почти без изменений (эталоны: tests/features/servers/snapshots).
Правила, SSH и база в service.py / api.py / repo.py; здесь только то, что работает с discord.Interaction.
Консоль открывается кнопками панели, её меню в фиче console.

Репозитории (ServersRepo, ConsoleRepo) views берут у ServersCog через клиент: `_repo(interaction.client)`.
Так persistent-панель работает после рестарта без глобальных подключений к базе.
"""

import asyncio
import logging
from datetime import datetime
from typing import ClassVar

import discord
from discord.ui import Button, Select, View

from lobby_bot.features.servers import service
from lobby_bot.features.servers.api import execute_ssh_command
from lobby_bot.features.servers.repo import ServersRepo
from lobby_bot.features.servers.service import (
    DEFAULT_MODIFIERS,
    DISABLED_SERVERS,
    HISTORY_CHANNEL_ID,
    MAP_NAMES,
    MOD_NAMES,
    MODIFIER_DESCRIPTIONS,
    PANEL_CHANNEL_ID,
    RANKED_MODS,
    RESERVATION_LOG_CHANNEL_ID,
    SERVERS,
    SERVERS_UNAVAILABLE_MESSAGE,
    all_servers_disabled,
    build_game_command,
    clear_current_host,
    clear_reservation,
    generate_elo_cache,
    generate_traitor_best_winrates,
    get_current_host,
    get_next_port,
    get_panel_message_id,
    get_reservation,
    get_uptime_text,
    get_user_modifiers,
    get_user_modifiers_raw,
    is_ranked_match,
    is_server_occupied,
    reserve_server,
    save_match_info,
    save_user_modifiers,
    save_user_preset,
    set_current_host,
    set_user_modifiers_enabled,
)
from lobby_bot.settings import settings

logger = logging.getLogger(__name__)

# Папка игры, куда ставится клиентская часть мода (инструкция в «Полезном»)
GAME_PAKS_PATH = "D:\\Games\\Social Deduction Game\\Game\\Content\\Paks"

# Тексты, которые показываются в нескольких местах (панель, /srv, настройки запуска)
RANKED_MODE_NOTE = (
    "📊 Ранговый режим активен при игре без модов или с любой комбинацией из: тишины на старте, рандомных ролей, "
    "квестов, 3/4/5 способностей. Если добавить любой другой мод — ранговый режим выключится. Чтобы выключить его "
    "намеренно — выберите мод **Chill**."
)
RELIC_CLIENT_NOTE = (
    "\n\n🗺️ *Для использования Островов или RelicMod от COMMUNITY необходима клиентская версия RelicMod 1.1.4, "
    "без неё — будет фаталить.*"
)

SITE_URL = settings.site_url
RELICMOD_DOWNLOAD_URL = settings.relicmod_download_url
TELEGRAM_CONTACT_URL = settings.telegram_contact_url
PATCHER_FIX_URL = settings.patcher_fix_url
HOST_REQUEST_CHANNEL_ID = settings.host_request_channel_id
HOST_REQUEST_PING_USER_ID = settings.host_request_ping_user_id


# ============ ПРОВЕРКИ ПО INTERACTION И SSH В ПОТОКЕ ============
async def check_server_status(server_num: int) -> bool:
    """Запущен ли сервер (SSH в потоке)."""
    return await asyncio.to_thread(service.server_running, server_num)


async def extract_match_guuid(server_num: int) -> str | None:
    """GUUID последнего матча из лога сервера (SSH в потоке)."""
    return await asyncio.to_thread(service.match_guuid, server_num)


def _member(interaction: discord.Interaction):
    return interaction.guild.get_member(interaction.user.id)


def check_user_has_permission(interaction: discord.Interaction) -> bool:
    """Роль хостера (HOSTER_ROLE_ID)."""
    return service.has_role(_member(interaction), service.HOSTER_ROLE_ID)


def is_moderator(interaction: discord.Interaction) -> bool:
    """Роль модератора (может останавливать любые серверы)."""
    return service.has_role(_member(interaction), service.MODERATOR_ROLE_ID)


def has_extended_access(interaction: discord.Interaction) -> bool:
    """Роль расширенного доступа (дополнительные моды и режимы)."""
    return service.has_role(_member(interaction), service.EXTENDED_ACCESS_ROLE_ID)


def _cog(client):
    """ServersCog держит репозитории фичи (создаются в setup из bot.db)."""
    return client.get_cog("ServersCog")


def _repo(client) -> ServersRepo:
    return _cog(client).repo


async def _clear_host(client, server_id: str) -> None:
    cog = _cog(client)
    await clear_current_host(cog.repo, cog.console_repo, server_id)


async def get_user_preset(repo: ServersRepo, user_id: str, interaction: discord.Interaction = None) -> dict:
    return await service.get_user_preset(repo, user_id, has_extended_access(interaction) if interaction else False)


async def hosts_any_server(repo: ServersRepo, user_id: str) -> bool:
    """Хостит ли пользователь хоть один сервер (проверка до первого совпадения)."""
    for scfg in SERVERS.values():
        if ((await get_current_host(repo, scfg["server_id"])) or {}).get("host_id") == user_id:
            return True
    return False


# ============ ПАНЕЛЬ ============
async def update_panel_embed(client):
    """Обновить embed панели управления"""
    try:
        repo = _repo(client)
        message_id = await get_panel_message_id(repo)
        if not message_id:
            return

        channel = client.get_channel(PANEL_CHANNEL_ID)
        if not channel:
            return

        message = await channel.fetch_message(message_id)

        # Проверяем реальный статус серверов и синхронизируем с БД
        for snum, scfg in SERVERS.items():
            sid = scfg["server_id"]
            running = await check_server_status(snum)
            sdata = await get_current_host(repo, sid)
            if not running and sdata and sdata.get("host_name"):
                await _clear_host(client, sid)
            elif running and (not sdata or not sdata.get("host_name")):
                await set_current_host(repo, sid, "unknown", "Неизвестно")

        description_parts = []

        for snum, scfg in SERVERS.items():
            sid = scfg["server_id"]
            sdata = await get_current_host(repo, sid)
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
                res = await get_reservation(repo, snum)
                if res:
                    description_parts.append(
                        f"**Сервер #{num_label} ({region_label}):** 🔒 Зарезервирован\n👤 {res['reserved_by_name']} до "
                        f"{res['reserved_until'].strftime('%H:%M')} UTC"
                    )
                else:
                    description_parts.append(f"**Сервер #{num_label} ({region_label}):** 🟢 Свободен")
            description_parts.append("")

        description_parts.append(f"\nВедётся подсчёт [статистики]({SITE_URL}/players_graph.html)")
        description_parts.append("")
        description_parts.append("</srv:100000000000000002> - ЗАПУСТИТЬ сервер")
        description_parts.append("</player:100000000000000001> - получить информацию об отдельном игроке")
        description_parts.append("</leaderboard:100000000000000007> - открыть ранговую таблицу")
        description_parts.append("")
        description_parts.append("</stop:100000000000000003> - ОСТАНОВИТЬ сервер")
        description_parts.append("</crew_win:100000000000000004> - засчитать ПОБЕДУ МИРНЫМ")
        description_parts.append("</traitors_win:100000000000000005> - засчитать ПОБЕДУ ПРЕДАТЕЛЯМ")
        description_parts.append("")
        description_parts.append("</useful_things:100000000000000006> - получить доп. информацию по игре")
        description_parts.append("</report:100000000000000008> - закинуть репортик")
        description_parts.append("")
        description_parts.append(RANKED_MODE_NOTE)

        embed = discord.Embed(
            title="🎮 Статус серверов Social Deduction Game",
            description="\n".join(description_parts),
            color=discord.Color.blue(),
        )

        await message.edit(embed=embed)
    except Exception as e:
        logger.exception(f"Ошибка обновления панели: {e}")


# ============ СЕЛЕКТЫ ============
class GameModeSelect(Select):
    def __init__(self, current_mode: str, parent_view):
        options = [
            discord.SelectOption(
                label="Обычный",
                value="normal",
                description="8 игроков, 2 предателя",
                emoji="👥",
                default=(current_mode == "normal"),
            ),
            discord.SelectOption(
                label="Соло",
                value="solo",
                description="1 игрок, 1 предатель",
                emoji="🎯",
                default=(current_mode == "solo"),
            ),
        ]
        super().__init__(options=options, row=0)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_mode = self.values[0]
        await self.parent_view.update_embed(interaction)


class IslandsPlayersSelect(Select):
    def __init__(self, current_players: int, parent_view, row: int):
        options = [
            discord.SelectOption(
                label="8 игроков",
                value="8",
                description="Стандартный размер",
                emoji="👥",
                default=(current_players == 8),
            ),
            discord.SelectOption(
                label="11 игроков",
                value="11",
                description="Расширенный состав",
                emoji="👥",
                default=(current_players == 11),
            ),
        ]
        super().__init__(placeholder="Кол-во игроков (Острова)", options=options, row=row)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.islands_players = int(self.values[0])
        await self.parent_view.update_embed(interaction)


class RegionSelect(Select):
    def __init__(self, parent_view, row: int):
        options = [
            discord.SelectOption(label="Франкфурт", value="frankfurt", description="Два сервера", default=True),
        ]
        super().__init__(options=options, row=row)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_region = self.values[0]
        await self.parent_view.update_embed(interaction)


class MapSelect(Select):
    def __init__(self, current_map: str, parent_view, row: int):
        options = [
            discord.SelectOption(
                label="Гавань",
                value="harbor",
                description="Карта: Harbor",
                emoji="🏔️",
                default=(current_map == "harbor"),
            ),
            discord.SelectOption(
                label="Долина",
                value="valley",
                description="Карта: Valley",
                emoji="🌊",
                default=(current_map == "valley"),
            ),
            discord.SelectOption(
                label="Застава",
                value="outpost",
                description="Карта: Outpost",
                emoji="⛰️",
                default=(current_map == "outpost"),
            ),
            discord.SelectOption(
                label="Острова",
                value="islands",
                description="Карта: Islands",
                emoji="🏝️",
                default=(current_map == "islands"),
            ),
        ]
        super().__init__(options=options, row=row)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_map = self.values[0]
        # При выборе Островов — принудительно включаем relic_mod и islands_11, убираем ability_4/ability_5
        if self.values[0] == "islands":
            if "relic_mod" not in self.parent_view.selected_mods:
                self.parent_view.selected_mods.append("relic_mod")
            if (
                "islands_11" not in self.parent_view.selected_mods
                and "islands_10" not in self.parent_view.selected_mods
            ):
                self.parent_view.selected_mods.append("islands_11")
            self.parent_view.selected_mods = [
                m for m in self.parent_view.selected_mods if m not in ("ability_4", "ability_5")
            ]
        else:
            # При смене карты убираем islands_11 и islands_10
            self.parent_view.selected_mods = [
                m for m in self.parent_view.selected_mods if m not in ("islands_11", "islands_10")
            ]
        await self.parent_view.update_embed(interaction)


class ModsSelect(Select):
    def __init__(self, current_mods: list, parent_view, row: int):
        # Определяем какие моды недоступны
        has_ability_4 = "ability_4" in current_mods
        has_ability_5 = "ability_5" in current_mods

        options = [
            discord.SelectOption(
                label="Рандомные роли",
                value="rand_role",
                description="Случайное распределение ролей",
                emoji="🎲",
                default=("rand_role" in current_mods),
            ),
            discord.SelectOption(
                label="Пять способностей",
                value="ability_5",
                description="Кастомные способности (5)" if not has_ability_4 else "Несовместимо с 4 способностями",
                emoji="✨",
                default=("ability_5" in current_mods),
            ),
            discord.SelectOption(
                label="Четыре способности",
                value="ability_4",
                description="Кастомные способности (4)" if not has_ability_5 else "Несовместимо с 5 способностями",
                emoji="💫",
                default=("ability_4" in current_mods),
            ),
            discord.SelectOption(
                label="Тишина на старте",
                value="ability_cast_start",
                description="Отключение голоса в начале",
                emoji="🔇",
                default=("ability_cast_start" in current_mods),
            ),
            discord.SelectOption(
                label="Console",
                value="console",
                description="Включить консоль",
                emoji="🎮",
                default=("console" in current_mods),
            ),
            discord.SelectOption(
                label="Ability Charge",
                value="ability_charge",
                description="Фикс заряда способностей",
                emoji="⚡",
                default=("ability_charge" in current_mods),
            ),
            discord.SelectOption(
                label="Квесты",
                value="quests",
                description="Мгновенные квесты",
                emoji="📜",
                default=("quests" in current_mods),
            ),
            discord.SelectOption(
                label="Chill",
                value="chill",
                description="Неранговый режим",
                emoji="😎",
                default=("chill" in current_mods),
            ),
            discord.SelectOption(
                label="RelicMod 1.1.4",
                value="relic_mod",
                description="Реликтовый мод (авто для Островов)",
                emoji="🗿",
                default=("relic_mod" in current_mods),
            ),
            discord.SelectOption(
                label="Guides",
                value="guides",
                description="Включить гайды",
                emoji="📖",
                default=("guides" in current_mods),
            ),
        ]

        # Добавляем моды для пользователей с расширенным доступом
        if parent_view.is_admin:
            options.append(
                discord.SelectOption(
                    label="Тренировочный мод",
                    value="train_mode",
                    description="Режим тренировки",
                    emoji="🎯",
                    default=("train_mode" in current_mods),
                )
            )
            options.append(
                discord.SelectOption(
                    label="Ability Upgrade",
                    value="ability_upgrade",
                    description="Улучшение способностей",
                    emoji="🔥",
                    default=("ability_upgrade" in current_mods),
                )
            )

        # Добавляем опции размера для Островов
        if parent_view.selected_map == "islands":
            options.append(
                discord.SelectOption(
                    label="Острова - 10 человек",
                    value="islands_10",
                    description="10 игроков вместо 8",
                    emoji="🔢",
                    default=("islands_10" in current_mods),
                )
            )
            options.append(
                discord.SelectOption(
                    label="Острова - 11 человек",
                    value="islands_11",
                    description="11 игроков вместо 8",
                    emoji="🔢",
                    default=("islands_11" in current_mods),
                )
            )

        super().__init__(options=options, min_values=0, max_values=len(options), row=row)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        selected = self.values

        # Проверяем конфликт между ability_4 и ability_5
        if "ability_4" in selected and "ability_5" in selected:
            # Определяем какой мод был добавлен последним
            old_mods = set(self.parent_view.selected_mods)
            new_mods = set(selected)
            added = new_mods - old_mods

            if "ability_4" in added:
                # Убираем ability_5
                selected = [m for m in selected if m != "ability_5"]
            elif "ability_5" in added:
                # Убираем ability_4
                selected = [m for m in selected if m != "ability_4"]

        # Острова — ability_4 и ability_5 нельзя добавить
        if self.parent_view.selected_map == "islands":
            selected = [m for m in selected if m not in ("ability_4", "ability_5")]

        # Конфликт islands_10 и islands_11 — берём последний добавленный
        if "islands_10" in selected and "islands_11" in selected:
            old_mods = set(self.parent_view.selected_mods)
            new_mods = set(selected)
            added = new_mods - old_mods
            if "islands_10" in added:
                selected = [m for m in selected if m != "islands_11"]
            else:
                selected = [m for m in selected if m != "islands_10"]

        self.parent_view.selected_mods = selected

        # Острова — relic_mod нельзя убрать
        if self.parent_view.selected_map == "islands" and "relic_mod" not in self.parent_view.selected_mods:
            self.parent_view.selected_mods.append("relic_mod")

        await self.parent_view.update_embed(interaction)


# ============ МОДАЛЬНОЕ ОКНО НАСТРОЕК ============
class ServerSettingsView(View):
    @classmethod
    async def create(cls, repo: ServersRepo, user_id: str, username: str, preset: dict) -> "ServerSettingsView":
        _, enabled = await get_user_modifiers_raw(repo, user_id)
        return cls(user_id, username, preset, modifiers_enabled=enabled)

    def __init__(self, user_id: str, username: str, preset: dict, modifiers_enabled: bool = False):
        super().__init__(timeout=2700)
        self.user_id = user_id
        self.username = username
        self.preset = preset
        self.selected_map = preset["map"]
        self.selected_mods = preset["mods"].copy()
        self.selected_mode = preset.get("mode", "normal")
        self.selected_region = preset.get("region", "frankfurt")
        self.is_admin = preset.get("is_admin", False)

        # Чистим островные моды если карта не Острова
        if self.selected_map != "islands":
            self.selected_mods = [m for m in self.selected_mods if m not in ("islands_11", "islands_10")]
        else:
            # Острова — убираем ability_4/ability_5 из пресета
            self.selected_mods = [m for m in self.selected_mods if m not in ("ability_4", "ability_5")]

        # Состояние пользовательских модификаторов читается заранее (create)
        self.custom_modifiers_enabled = modifiers_enabled

        current_row = 0

        # Добавляем селект для режима (только для админов)
        if self.is_admin:
            self.add_item(GameModeSelect(self.selected_mode, self))
            current_row = 1
            # Админы тоже выбирают регион
            self.add_item(RegionSelect(self, row=current_row))
            current_row = 2
        else:
            # Для обычных пользователей добавляем селект региона
            self.add_item(RegionSelect(self, row=0))
            current_row = 1

        # Добавляем селект для карты
        self.add_item(MapSelect(self.selected_map, self, row=current_row))

        # Добавляем селект для модов
        self.add_item(ModsSelect(self.selected_mods, self, row=current_row + 1))

        # Добавляем кнопки
        confirm_btn = Button(label="✅ Запустить сервер", style=discord.ButtonStyle.success, row=4)
        confirm_btn.callback = self.confirm_button_callback
        self.add_item(confirm_btn)

        modifiers_label = "⚙️ Модификаторы: ВКЛ" if self.custom_modifiers_enabled else "⚙️ Модификаторы: ВЫКЛ"
        modifiers_style = (
            discord.ButtonStyle.primary if self.custom_modifiers_enabled else discord.ButtonStyle.secondary
        )
        modifiers_btn = Button(label=modifiers_label, style=modifiers_style, row=4)
        modifiers_btn.callback = self.modifiers_button_callback
        self.add_item(modifiers_btn)

        cancel_btn = Button(label="❌ Отмена", style=discord.ButtonStyle.secondary, row=4)
        cancel_btn.callback = self.cancel_button_callback
        self.add_item(cancel_btn)

    def get_initial_embed(self):
        """Получить начальный embed с отображением рангового режима"""
        is_ranked = (
            is_ranked_match(self.selected_mods, self.selected_mode, self.selected_map)
            and not self.custom_modifiers_enabled
        )

        # Формируем текст рангового режима
        if is_ranked:
            ranked_text = "✅ Да"
            ranked_info = ""
        else:
            # Определяем причину нерангового режима
            non_ranked_reasons = []

            if self.custom_modifiers_enabled:
                non_ranked_reasons.append("пользовательские модификаторы")

            if self.selected_mode != "normal":
                non_ranked_reasons.append("Соло режим")

            # Проверяем неранговые моды
            non_ranked_mods = [mod for mod in self.selected_mods if mod not in RANKED_MODS]
            if non_ranked_mods:
                mods_names = [MOD_NAMES.get(m, m) for m in non_ranked_mods]
                non_ranked_reasons.append(f"моды: {', '.join(mods_names)}")

            reason_text = ", ".join(non_ranked_reasons) if non_ranked_reasons else "неизвестная причина"
            ranked_text = f"❌ Нет ({reason_text})"

            # Добавляем предупреждение о Chill моде
            if "chill" in self.selected_mods:
                ranked_info = (
                    "\n\n⚠️ *Если выбран мод Chill, результаты матча не будут учитываться в эло-рейтинговой таблице.*"
                )
            else:
                ranked_info = ""

        modifiers_info = "\n**Модификаторы:** ✅ Включены" if self.custom_modifiers_enabled else ""
        islands_info = RELIC_CLIENT_NOTE if "relic_mod" in self.selected_mods else ""

        return discord.Embed(
            title="⚙️ Настройки сервера",
            description=(
                "Сохраняется последний выбор комбинации модов, карт, модификаторов.\n"
                f"На сервере ведётся подсчет статистики: {SITE_URL}/players_graph.html\n\n"
                f"**Ранговый режим:** {ranked_text}"
                f"{ranked_info}"
                f"{modifiers_info}"
                f"{islands_info}"
            ),
            color=discord.Color.blue(),
        )

    async def update_embed(self, interaction: discord.Interaction):
        """Обновить embed с текущими настройками"""
        # Проверяем является ли матч ranked
        is_ranked = (
            is_ranked_match(self.selected_mods, self.selected_mode, self.selected_map)
            and not self.custom_modifiers_enabled
        )

        # Формируем текст рангового режима
        if is_ranked:
            ranked_text = "✅ Да"
            ranked_info = ""
        else:
            # Определяем причину нерангового режима
            non_ranked_reasons = []

            if self.custom_modifiers_enabled:
                non_ranked_reasons.append("пользовательские модификаторы")

            if self.selected_mode != "normal":
                non_ranked_reasons.append("Соло режим")

            # Проверяем неранговые моды
            non_ranked_mods = [mod for mod in self.selected_mods if mod not in RANKED_MODS]
            if non_ranked_mods:
                mods_names = [MOD_NAMES.get(m, m) for m in non_ranked_mods]
                non_ranked_reasons.append(f"моды: {', '.join(mods_names)}")

            reason_text = ", ".join(non_ranked_reasons) if non_ranked_reasons else "неизвестная причина"
            ranked_text = f"❌ Нет ({reason_text})"

            # Добавляем предупреждение о Chill моде
            if "chill" in self.selected_mods:
                ranked_info = (
                    "\n\n⚠️ *Если выбран мод Chill, результаты матча не будут учитываться в эло-рейтинговой таблице.*"
                )
            else:
                ranked_info = ""

        modifiers_info = "\n**Модификаторы:** ✅ Включены" if self.custom_modifiers_enabled else ""
        islands_info = RELIC_CLIENT_NOTE if "relic_mod" in self.selected_mods else ""

        embed = discord.Embed(
            title="⚙️ Настройки сервера",
            description=(
                "Сохраняется последний выбор комбинации модов, карт, модификаторов.\n"
                f"На сервере ведётся подсчет статистики: {SITE_URL}/players_graph.html\n\n"
                f"**Ранговый режим:** {ranked_text}"
                f"{ranked_info}"
                f"{modifiers_info}"
                f"{islands_info}"
            ),
            color=discord.Color.blue(),
        )

        # Пересоздаем view с обновленными селектами
        self.clear_items()

        current_row = 0
        if self.is_admin:
            self.add_item(GameModeSelect(self.selected_mode, self))
            current_row = 1
            # Админы тоже выбирают регион
            self.add_item(RegionSelect(self, row=current_row))
            current_row = 2
        else:
            # Для обычных пользователей добавляем селект региона
            self.add_item(RegionSelect(self, row=0))
            current_row = 1

        self.add_item(MapSelect(self.selected_map, self, row=current_row))
        self.add_item(ModsSelect(self.selected_mods, self, row=current_row + 1))

        # Добавляем кнопки обратно
        confirm_btn = Button(label="✅ Запустить сервер", style=discord.ButtonStyle.success, row=4)
        confirm_btn.callback = self.confirm_button_callback
        self.add_item(confirm_btn)

        modifiers_label = "⚙️ Модификаторы: ВКЛ" if self.custom_modifiers_enabled else "⚙️ Модификаторы: ВЫКЛ"
        modifiers_style = (
            discord.ButtonStyle.primary if self.custom_modifiers_enabled else discord.ButtonStyle.secondary
        )
        modifiers_btn = Button(label=modifiers_label, style=modifiers_style, row=4)
        modifiers_btn.callback = self.modifiers_button_callback
        self.add_item(modifiers_btn)

        cancel_btn = Button(label="❌ Отмена", style=discord.ButtonStyle.secondary, row=4)
        cancel_btn.callback = self.cancel_button_callback
        self.add_item(cancel_btn)

        await interaction.response.edit_message(embed=embed, view=self)

    async def confirm_button_callback(self, interaction: discord.Interaction):
        message_deleted = False

        # Убираем кнопки и меняем на статус
        try:
            await interaction.response.edit_message(
                content="🔄 Проверяю доступность серверов...", embed=None, view=None
            )
        except discord.NotFound:
            message_deleted = True
            await interaction.followup.send(
                "⚠️ Вы закрыли меню, но сервер всё равно запускается. Ожидайте...", ephemeral=True
            )

        try:
            # Шаг 1: Проверяем доступность серверов
            # Выбираем свободный сервер
            selected_server = None
            user_id_str = self.user_id

            for server_num in SERVERS:
                if server_num in DISABLED_SERVERS:
                    continue
                if await is_server_occupied(_repo(interaction.client), server_num, user_id_str):
                    continue
                running = await check_server_status(server_num)
                if not running:
                    selected_server = server_num
                    break

            if selected_server is None:
                region_name = "Франкфурт"
                if all_servers_disabled():
                    unavailable_text = SERVERS_UNAVAILABLE_MESSAGE
                else:
                    unavailable_text = (
                        f"⛔ Все серверы в **{region_name}** заняты!\n"
                        f"Дождитесь освобождения или выберите другую локацию."
                    )
                if message_deleted:
                    await interaction.followup.send(unavailable_text, ephemeral=True)
                else:
                    await interaction.edit_original_response(content=unavailable_text)
                return

            server_config = SERVERS[selected_server]
            server_name = server_config["name"]
            # Получаем следующий порт по ротации
            server_id = server_config["server_id"]
            next_port = await get_next_port(_repo(interaction.client), server_id)
            port = next_port
            ip = server_config["ip"]

            # Шаг 2: Сервер свободен, начинаем запуск
            if message_deleted:
                await interaction.followup.send(
                    f"✅ {server_name} (порт {port}) свободен!\n🚀 Запускаю сервер...", ephemeral=True
                )
            else:
                await interaction.edit_original_response(
                    content=f"✅ {server_name} (порт {port}) свободен!\n🚀 Запускаю сервер..."
                )

            # Получаем реальный IP сервера
            loop = asyncio.get_event_loop()
            ip_cmd = "curl -s ifconfig.me"
            try:
                real_ip, _ = await loop.run_in_executor(None, execute_ssh_command, ip_cmd, selected_server)
                real_ip = real_ip.strip() if real_ip else ip
            except Exception:
                real_ip = ip  # Используем IP из конфига

            # Сохраняем пресет
            repo = _repo(interaction.client)
            await save_user_preset(
                repo, self.user_id, self.selected_map, self.selected_mods, self.selected_mode, self.selected_region
            )

            # Получаем пользовательские модификаторы если включены
            active_modifiers = await get_user_modifiers(repo, self.user_id) if self.custom_modifiers_enabled else None

            # Собираем команду
            game_cmd = build_game_command(
                self.selected_map,
                self.selected_mods,
                self.selected_mode,
                selected_server,
                custom_modifiers=active_modifiers,
                port=next_port,
            )

            logger.debug(f"DEBUG: Команда запуска: {game_cmd}")

            # Загружаем ELO кэш на сервер если ranked
            cog = _cog(interaction.client)
            await generate_elo_cache(cog.repo, cog.elo_repo, cog.seasons, selected_server)

            # Генерируем файл винрейтов пар на предателях
            await generate_traitor_best_winrates(_repo(interaction.client), selected_server)

            # Сохраняем хоста и снимаем резервацию
            server_id = server_config["server_id"]
            await set_current_host(_repo(interaction.client), server_id, self.user_id, self.username, port)
            await clear_reservation(_repo(interaction.client), selected_server)
            logger.debug(f"DEBUG: Установлен хост {self.username} для {server_id}")

            # Шаг 3: Запускаем сервер
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, execute_ssh_command, game_cmd, selected_server)
            except Exception as ssh_err:
                logger.exception(f"DEBUG: SSH ошибка при запуске команды: {ssh_err}")

            await asyncio.sleep(3)

            # Шаг 4: Ожидаем загрузки
            status_msg = f"⏳ Ожидание загрузки {server_name}...\n⏱️ Среднее время: 15 секунд"
            if message_deleted:
                # Обновляем последнее followup сообщение
                pass  # followup нельзя редактировать просто так
            else:
                await interaction.edit_original_response(content=status_msg)

            max_wait = 60
            check_interval = 3
            log_file = f"{server_config['folder']}/WindowsServer/Game/Saved/Logs/Game.log"

            for i in range(max_wait // check_interval):
                await asyncio.sleep(check_interval)

                log_cmd = f"cat {log_file}"
                try:
                    log_out, _ = await loop.run_in_executor(None, execute_ssh_command, log_cmd, selected_server)
                except Exception as ssh_err:
                    logger.exception(f"DEBUG: SSH ошибка при чтении лога: {ssh_err}")
                    log_out = ""

                if "(Engine Initialization) Total time:" in log_out:
                    # Успешный запуск
                    map_name = MAP_NAMES[self.selected_map]

                    if self.selected_mods:
                        mods_names = [MOD_NAMES[m] for m in self.selected_mods]
                        mods_text = ", ".join(mods_names)
                    else:
                        mods_text = "Без модов"

                    # Ждем несколько секунд чтобы GUUID записался в лог
                    await asyncio.sleep(5)

                    # Извлекаем GUUID и сохраняем информацию о матче
                    guuid = await extract_match_guuid(selected_server)
                    if guuid:
                        await save_match_info(
                            _repo(interaction.client), guuid, self.selected_mods, self.selected_mode, self.selected_map
                        )
                    else:
                        logger.warning(f"DEBUG: GUUID не найден в логах сервера {selected_server}")

                    # Создаём embed
                    embed = discord.Embed(
                        title="Адрес сервера",
                        description=(
                            f"**Айпи:**\n```\n{real_ip}\n```\n"
                            f"**Порт:**\n```\n{port}\n```\n"
                            f"**Время запуска:** ~{(i + 1) * check_interval} сек\n"
                            f"**Карта:** {map_name}\n"
                            f"**Моды:** {mods_text}"
                        ),
                        color=discord.Color.green(),
                    )

                    # Создаём view с кнопкой остановки
                    stop_view = StopServerView(selected_server, self.user_id)

                    # Обновляем панель после успешного запуска
                    await update_panel_embed(interaction.client)
                    logger.debug("DEBUG: Панель обновлена после успешного запуска")

                    # Отправляем лог в канал истории
                    history_channel = interaction.client.get_channel(HISTORY_CHANNEL_ID)
                    if history_channel:
                        region_name = server_config.get("region_name", f"Сервер #{selected_server + 1}")
                        is_ranked_history = (
                            is_ranked_match(self.selected_mods, self.selected_mode, self.selected_map)
                            and not self.custom_modifiers_enabled
                        )

                        history_embed = discord.Embed(title="🎮 Сервер запущен", color=discord.Color.green())
                        history_embed.add_field(name="Сервер", value=region_name, inline=False)
                        history_embed.add_field(name="Хост", value=self.username, inline=False)
                        history_embed.add_field(name="Карта", value=map_name, inline=False)
                        mode_text = "🎯 Соло" if self.selected_mode == "solo" else "👥 Обычный"
                        history_embed.add_field(name="Тип", value=mode_text, inline=False)
                        history_embed.add_field(
                            name="Моды", value=mods_text if mods_text else "Без модов", inline=False
                        )
                        history_embed.add_field(
                            name="Ранговый режим", value="✅ Да" if is_ranked_history else "❌ Нет", inline=False
                        )

                        if self.custom_modifiers_enabled and active_modifiers:
                            mod_lines = []
                            for key, desc in MODIFIER_DESCRIPTIONS.items():
                                val = active_modifiers.get(key, DEFAULT_MODIFIERS[key])
                                default_val = DEFAULT_MODIFIERS[key]
                                if val != default_val:
                                    mod_lines.append(f"{desc}: {val}")
                            if mod_lines:
                                history_embed.add_field(name="Модификаторы", value="\n".join(mod_lines), inline=False)
                            else:
                                history_embed.add_field(
                                    name="Модификаторы", value="✅ Включены (все по умолчанию)", inline=False
                                )
                        else:
                            history_embed.add_field(name="Модификаторы", value="❌ Нет", inline=False)

                        try:
                            await history_channel.send(embed=history_embed)
                        except Exception:  # канал истории не главное: запуск сервера не прерываем
                            logger.exception("Не удалось отправить запуск сервера в канал истории")

                    if message_deleted:
                        await interaction.followup.send(embed=embed, view=stop_view, ephemeral=True)
                    else:
                        await interaction.edit_original_response(content=None, embed=embed, view=stop_view)
                    return

            # Таймаут загрузки
            map_name = MAP_NAMES[self.selected_map]

            if self.selected_mods:
                mods_names = [MOD_NAMES[m] for m in self.selected_mods]
                mods_text = ", ".join(mods_names)
            else:
                mods_text = "Без модов"

            # Создаём embed
            embed = discord.Embed(
                title="⚠️ Не удалось подтвердить загрузку",
                description=(
                    f"**Айпи:**\n```\n{real_ip}\n```\n"
                    f"**Порт:**\n```\n{port}\n```\n"
                    f"**Время запуска:** ~{max_wait} сек (таймаут)\n"
                    f"**Карта:** {map_name}\n"
                    f"**Моды:** {mods_text}"
                ),
                color=discord.Color.orange(),
            )

            # Создаём view с кнопкой остановки
            stop_view = StopServerView(selected_server, self.user_id)

            # Обновляем панель даже при таймауте
            await update_panel_embed(interaction.client)
            logger.debug("DEBUG: Панель обновлена после таймаута")

            if message_deleted:
                await interaction.followup.send(embed=embed, view=stop_view, ephemeral=True)
            else:
                await interaction.edit_original_response(content=None, embed=embed, view=stop_view)

        except Exception as e:
            error_msg = f"❌ Ошибка при запуске: {e}"
            if message_deleted:
                await interaction.followup.send(error_msg, ephemeral=True)
            else:
                await interaction.edit_original_response(content=error_msg)

    async def modifiers_button_callback(self, interaction: discord.Interaction):
        """Показать информацию о пользовательских модификаторах и дать переключить"""
        modifiers, enabled = await get_user_modifiers_raw(_repo(interaction.client), self.user_id)

        if not modifiers:
            embed = discord.Embed(
                title="⚙️ Пользовательские модификаторы",
                description=(
                    "Пользовательские модификаторы не найдены.\n\n"
                    "Используйте команду </usermodifiers:100000000000000009> для настройки."
                ),
                color=discord.Color.orange(),
            )
            # Показываем в том же сообщении с кнопкой "Назад"
            back_view = View(timeout=60)
            back_btn = Button(label="◀️ Назад", style=discord.ButtonStyle.secondary)
            parent_view = self

            async def back_from_empty(btn_interaction: discord.Interaction):
                await parent_view._refresh_main_menu(btn_interaction)

            back_btn.callback = back_from_empty
            back_view.add_item(back_btn)
            await interaction.response.edit_message(embed=embed, view=back_view)
            return

        # Модификаторы есть — показываем текущие значения и кнопки вкл/выкл
        lines = []
        for key, desc in MODIFIER_DESCRIPTIONS.items():
            val = modifiers.get(key, DEFAULT_MODIFIERS[key])
            indicator = " ✏️" if val != DEFAULT_MODIFIERS[key] else ""
            lines.append(f"**{desc}:** {val}{indicator}")

        status = "✅ Включены" if enabled else "❌ Выключены"
        embed = discord.Embed(
            title="⚙️ Пользовательские модификаторы",
            description=f"**Статус:** {status}\n\n"
            + "\n".join(lines)
            + "\n\nДля изменения используйте </usermodifiers:100000000000000009>",
            color=discord.Color.blue() if enabled else discord.Color.greyple(),
        )

        toggle_label = "🔴 Выключить модификаторы" if enabled else "🟢 Включить модификаторы"
        toggle_style = discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success

        toggle_view = View(timeout=60)
        toggle_btn = Button(label=toggle_label, style=toggle_style)
        reset_btn = Button(label="🔄 Сбросить", style=discord.ButtonStyle.secondary)
        back_btn = Button(label="◀️ Назад", style=discord.ButtonStyle.secondary)

        parent_view = self

        async def toggle_callback(btn_interaction: discord.Interaction):
            new_enabled = not parent_view.custom_modifiers_enabled
            await set_user_modifiers_enabled(_repo(btn_interaction.client), parent_view.user_id, new_enabled)
            parent_view.custom_modifiers_enabled = new_enabled
            # Возвращаемся в основное меню прямо в том же сообщении
            await parent_view._refresh_main_menu(btn_interaction)

        async def reset_callback(btn_interaction: discord.Interaction):
            await save_user_modifiers(_repo(btn_interaction.client), parent_view.user_id, DEFAULT_MODIFIERS.copy())
            await parent_view.modifiers_button_callback(btn_interaction)

        async def back_callback(btn_interaction: discord.Interaction):
            await parent_view._refresh_main_menu(btn_interaction)

        toggle_btn.callback = toggle_callback
        reset_btn.callback = reset_callback
        back_btn.callback = back_callback
        toggle_view.add_item(toggle_btn)
        toggle_view.add_item(reset_btn)
        toggle_view.add_item(back_btn)

        await interaction.response.edit_message(embed=embed, view=toggle_view)

    async def _refresh_main_menu(self, interaction: discord.Interaction):
        """Обновить основное меню настроек после изменения модификаторов"""
        is_ranked = (
            is_ranked_match(self.selected_mods, self.selected_mode, self.selected_map)
            and not self.custom_modifiers_enabled
        )

        if is_ranked:
            ranked_text = "✅ Да"
            ranked_info = ""
        else:
            non_ranked_reasons = []
            if self.custom_modifiers_enabled:
                non_ranked_reasons.append("пользовательские модификаторы")
            if self.selected_mode != "normal":
                non_ranked_reasons.append("Соло режим")
            non_ranked_mods = [mod for mod in self.selected_mods if mod not in RANKED_MODS]
            if non_ranked_mods:
                mods_names = [MOD_NAMES.get(m, m) for m in non_ranked_mods]
                non_ranked_reasons.append(f"моды: {', '.join(mods_names)}")
            reason_text = ", ".join(non_ranked_reasons) if non_ranked_reasons else "неизвестная причина"
            ranked_text = f"❌ Нет ({reason_text})"
            if "chill" in self.selected_mods:
                ranked_info = (
                    "\n\n⚠️ *Если выбран мод Chill, результаты матча не будут учитываться в эло-рейтинговой таблице.*"
                )
            else:
                ranked_info = ""

        modifiers_info = "\n**Модификаторы:** ✅ Включены" if self.custom_modifiers_enabled else ""
        islands_info = RELIC_CLIENT_NOTE if "relic_mod" in self.selected_mods else ""

        embed = discord.Embed(
            title="⚙️ Настройки сервера",
            description=(
                "Сохраняется последний выбор комбинации модов, карт, модификаторов.\n"
                f"На сервере ведётся подсчет статистики: {SITE_URL}/players_graph.html\n\n"
                f"**Ранговый режим:** {ranked_text}"
                f"{ranked_info}"
                f"{modifiers_info}"
                f"{islands_info}"
            ),
            color=discord.Color.blue(),
        )

        # Пересоздаём кнопки
        self.clear_items()
        current_row = 0
        if self.is_admin:
            self.add_item(GameModeSelect(self.selected_mode, self))
            current_row = 1
            self.add_item(RegionSelect(self, row=current_row))
            current_row = 2
        else:
            self.add_item(RegionSelect(self, row=0))
            current_row = 1
        self.add_item(MapSelect(self.selected_map, self, row=current_row))
        self.add_item(ModsSelect(self.selected_mods, self, row=current_row + 1))

        confirm_btn = Button(label="✅ Запустить сервер", style=discord.ButtonStyle.success, row=4)
        confirm_btn.callback = self.confirm_button_callback
        self.add_item(confirm_btn)

        modifiers_label = "⚙️ Модификаторы: ВКЛ" if self.custom_modifiers_enabled else "⚙️ Модификаторы: ВЫКЛ"
        modifiers_style = (
            discord.ButtonStyle.primary if self.custom_modifiers_enabled else discord.ButtonStyle.secondary
        )
        modifiers_btn = Button(label=modifiers_label, style=modifiers_style, row=4)
        modifiers_btn.callback = self.modifiers_button_callback
        self.add_item(modifiers_btn)

        cancel_btn = Button(label="❌ Отмена", style=discord.ButtonStyle.secondary, row=4)
        cancel_btn.callback = self.cancel_button_callback
        self.add_item(cancel_btn)

        # Обновляем основное сообщение прямо в текущем interaction
        await interaction.response.edit_message(embed=embed, view=self)

    async def cancel_button_callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="❌ Запуск отменён", embed=None, view=None)


# ============ ПАНЕЛЬ УПРАВЛЕНИЯ ============
class StopServerView(View):
    last_match_end_time: ClassVar[dict[int, datetime]] = {}
    """View с кнопками управления сервером для конкретного пользователя"""

    def __init__(self, server_num: int, user_id: str):
        super().__init__(timeout=None)
        self.server_num = server_num
        self.user_id = user_id

    async def _end_match(self, interaction: discord.Interaction, winner: int):
        """Общая логика завершения матча. winner: 1 = мирные, 2 = предатели"""
        user_id = str(interaction.user.id)

        if user_id != self.user_id:
            await interaction.response.send_message("❌ Вы не можете завершить чужой матч!", ephemeral=True)
            return

        # Проверяем через БД что нажимающий — текущий хост сервера
        if not is_moderator(interaction):
            server_id = SERVERS[self.server_num]["server_id"]
            current = await get_current_host(_repo(interaction.client), server_id)
            if current and current.get("host_id") and current.get("host_id") != user_id:
                await interaction.response.send_message(
                    f"❌ Сервер сейчас принадлежит другому хосту ({current.get('host_name', '?')}). Вы не можете "
                    "завершить матч.",
                    ephemeral=True,
                )
                return

        now = service._now()

        # === КУЛДАУН 30 СЕК ===
        last_time = StopServerView.last_match_end_time.get(self.server_num)

        if last_time and (now - last_time).total_seconds() < 30:
            remaining = int(30 - (now - last_time).total_seconds())
            await interaction.response.send_message(
                f"⏳ Можно использовать снова через {remaining} сек.", ephemeral=True
            )
            return

        await interaction.response.defer()

        try:
            server_config = SERVERS[self.server_num]
            screen_name = server_config["screen_name"]
            folder = server_config["folder"]
            log_file = f"{folder}/WindowsServer/Game/Saved/Logs/Game.log"

            loop = asyncio.get_event_loop()

            # === ПРОВЕРКА ИГРОКОВ ===
            log_cmd = f"grep 'Join succeeded' {log_file} || true"
            log_out, _ = await loop.run_in_executor(None, execute_ssh_command, log_cmd, self.server_num)

            if "Join succeeded" not in log_out:
                await interaction.followup.send("❌ В лобби нет игроков. Завершение матча невозможно.", ephemeral=True)
                return

            # === ОТПРАВЛЯЕМ КОМАНДУ С ПОБЕДИТЕЛЕМ ===
            cmd1 = f"screen -S {screen_name} -p 0 -X stuff 'force_match_end.js?winner={winner}'"
            cmd2 = f"screen -S {screen_name} -p 0 -X stuff $'\r'"

            await loop.run_in_executor(None, execute_ssh_command, cmd1, self.server_num)
            await loop.run_in_executor(None, execute_ssh_command, cmd2, self.server_num)

            # сохраняем время кулдауна
            StopServerView.last_match_end_time[self.server_num] = now

            winner_text = "🏳️ Победа мирных" if winner == 1 else "☠️ Победа предателей"
            embed = discord.Embed(
                title=f"🏁 Матч завершён — {winner_text}",
                description="Игра была завершена принудительно.",
                color=discord.Color.orange(),
            )

            await interaction.edit_original_response(content=None, embed=embed, view=None)

            # Лог в канал истории
            log_channel = interaction.client.get_channel(HISTORY_CHANNEL_ID)
            if log_channel:
                log_embed = discord.Embed(
                    title=f"🏁 Матч завершён принудительно — {winner_text}", color=discord.Color.orange()
                )
                log_embed.add_field(name="Сервер", value=server_config['name'], inline=False)
                log_embed.add_field(
                    name="Завершил", value=f"{interaction.user.mention} ({interaction.user.display_name})", inline=False
                )
                log_embed.set_footer(text=service._now().strftime("%d.%m.%Y %H:%M UTC"))
                await log_channel.send(embed=log_embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)

    @discord.ui.button(label="🏳️ Победа мирных", style=discord.ButtonStyle.secondary, row=0)
    async def end_match_innocents(self, interaction: discord.Interaction, button: Button):
        await self._end_match(interaction, winner=1)

    @discord.ui.button(label="☠️ Победа предателей", style=discord.ButtonStyle.secondary, row=0)
    async def end_match_traitors(self, interaction: discord.Interaction, button: Button):
        await self._end_match(interaction, winner=2)

    @discord.ui.button(label="⏹️ Остановить сервер", style=discord.ButtonStyle.danger, row=1)
    async def stop_button(self, interaction: discord.Interaction, button: Button):

        user_id = str(interaction.user.id)

        # Проверяем что нажимающий — владелец кнопки
        if user_id != self.user_id:
            await interaction.response.send_message("❌ Вы не можете остановить чужой сервер!", ephemeral=True)
            return

        # Проверяем что в БД текущий хост — тот же пользователь (не перезаписан другим)
        if not is_moderator(interaction):
            server_id = SERVERS[self.server_num]["server_id"]
            current = await get_current_host(_repo(interaction.client), server_id)
            if current and current.get("host_id") and current.get("host_id") != user_id:
                await interaction.response.send_message(
                    f"❌ Сервер сейчас принадлежит другому хосту ({current.get('host_name', '?')}). Вы не можете его "
                    "остановить.",
                    ephemeral=True,
                )
                return

        await interaction.response.defer()

        try:
            server_config = SERVERS[self.server_num]
            server_name = server_config["name"]
            screen_name = server_config["screen_name"]
            server_id = server_config["server_id"]

            stop_cmd = f"screen -S {screen_name} -X quit"

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, execute_ssh_command, stop_cmd, self.server_num)

            await _clear_host(interaction.client, server_id)
            await update_panel_embed(interaction.client)

            stop_embed = discord.Embed(
                title="🔴 Сервер остановлен", description=f"{server_name} был остановлен.", color=discord.Color.red()
            )

            await interaction.edit_original_response(content=None, embed=stop_embed, view=None)

            # Лог в канал истории
            log_channel = interaction.client.get_channel(HISTORY_CHANNEL_ID)
            if log_channel:
                log_embed = discord.Embed(title="⏹️ Сервер остановлен", color=discord.Color.red())
                log_embed.add_field(name="Сервер", value=server_name, inline=False)
                log_embed.add_field(
                    name="Остановил",
                    value=f"{interaction.user.mention} ({interaction.user.display_name})",
                    inline=False,
                )
                log_embed.set_footer(text=service._now().strftime("%d.%m.%Y %H:%M UTC"))
                await log_channel.send(embed=log_embed)

        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка при остановке: {e}", ephemeral=True)


class ReservationRegionView(View):
    """View для резервации сервера (Франкфурт)"""

    def __init__(self, user_id: str, username: str):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.username = username
        # Автоматически резервируем при создании
        # Кнопки: Зарезервировать и Отмена

    @discord.ui.button(label="🌍 Зарезервировать (Франкфурт)", style=discord.ButtonStyle.primary, row=0)
    async def reserve_frankfurt(self, interaction: discord.Interaction, button: Button):
        await self._reserve(interaction)

    @discord.ui.button(label="❌ Отмена", style=discord.ButtonStyle.secondary, row=0)
    async def cancel(self, interaction: discord.Interaction, button: Button):
        await interaction.response.edit_message(content="❌ Резервация отменена.", view=None)

    async def _reserve(self, interaction: discord.Interaction):
        user_id = self.user_id
        username = self.username
        candidates = list(SERVERS.keys())
        region_name = "Франкфурт"

        # Проверяем нет ли уже резервации этого пользователя
        for snum in SERVERS:
            res = await get_reservation(_repo(interaction.client), snum)
            if res and res["reserved_by_id"] == user_id:
                await interaction.response.edit_message(
                    content="❌ У вас уже есть активная резервация! Сначала снимите её.", view=None
                )
                return

        # Выбираем первый свободный (не запущен и не зарезервирован)
        selected = None
        for snum in candidates:
            if snum in DISABLED_SERVERS:
                continue
            res = await get_reservation(_repo(interaction.client), snum)
            if res:
                continue
            doc = await get_current_host(_repo(interaction.client), SERVERS[snum]["server_id"])
            if doc and doc.get("host_name"):
                continue
            selected = snum
            break

        if selected is None:
            if all_servers_disabled():
                content = SERVERS_UNAVAILABLE_MESSAGE
            else:
                content = f"⛔ Все серверы в **{region_name}** заняты или уже зарезервированы."
            await interaction.response.edit_message(content=content, view=None)
            return

        reserved_until = await reserve_server(_repo(interaction.client), selected, user_id, username)

        server_name = SERVERS[selected]["name"]

        await interaction.response.edit_message(
            content=(
                f"🔒 **{server_name} ({region_name})** зарезервирован за **{username}**!\n"
                f"⏰ Резервация действует до **{reserved_until.strftime('%H:%M')} UTC** (10 минут)\n"
                "Запустите сервер через `/srv` — зарезервированный слот будет приоритетным и никто не сможет "
                "захостить ваш сервер."
            ),
            view=None,
        )

        # Уведомление в лог-канал
        log_channel = interaction.client.get_channel(RESERVATION_LOG_CHANNEL_ID)
        if log_channel:
            embed = discord.Embed(title="🔒 Сервер зарезервирован", color=discord.Color.orange())
            embed.add_field(name="Сервер", value=f"{server_name} ({region_name})", inline=False)
            embed.add_field(name="Пользователь", value=f"{interaction.user.mention} ({username})", inline=False)
            embed.add_field(name="До", value=f"{reserved_until.strftime('%H:%M')} UTC", inline=False)
            try:
                await log_channel.send(embed=embed)
            except Exception:  # лог резервов не главное: резерв уже сохранён
                logger.exception("Не удалось отправить в лог резервов")

        # Обновляем панель
        await update_panel_embed(interaction.client)


class CancelReservationView(View):
    """View для снятия своей резервации"""

    def __init__(self, server_num: int, user_id: str):
        super().__init__(timeout=60)
        self.server_num = server_num
        self.user_id = user_id

    @discord.ui.button(label="🔓 Снять резервацию", style=discord.ButtonStyle.danger)
    async def cancel_reservation(self, interaction: discord.Interaction, button: Button):
        if str(interaction.user.id) != self.user_id and not is_moderator(interaction):
            await interaction.response.send_message("❌ Это не ваша резервация.", ephemeral=True)
            return

        server_name = SERVERS[self.server_num]["name"]
        region_name = SERVERS[self.server_num]["region_name"]

        await clear_reservation(_repo(interaction.client), self.server_num)
        await interaction.response.edit_message(content="✅ Резервация снята.", view=None)

        # Уведомление в лог-канал
        log_channel = interaction.client.get_channel(RESERVATION_LOG_CHANNEL_ID)
        if log_channel:
            embed = discord.Embed(title="🔓 Резервация снята", color=discord.Color.light_grey())
            embed.add_field(name="Сервер", value=f"{server_name} ({region_name})", inline=False)
            embed.add_field(name="Снял", value=f"{interaction.user.mention} ({interaction.user.name})", inline=False)
            try:
                await log_channel.send(embed=embed)
            except Exception:  # лог резервов не главное: резерв уже сохранён
                logger.exception("Не удалось отправить в лог резервов")

        await update_panel_embed(interaction.client)

    @discord.ui.button(label="❌ Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: Button):
        await interaction.response.edit_message(content="Отменено.", view=None)


def build_server_info_embed(server_num: int, sdata: dict) -> discord.Embed:
    """Embed с айпи/портом и статусом запущенного сервера."""
    scfg = SERVERS[server_num]
    host_name = sdata.get("host_name", "Неизвестно") if sdata else "Неизвестно"
    started_at = sdata.get("started_at") if sdata else None
    uptime = get_uptime_text(started_at) if started_at else None
    # Реальный (ротируемый) порт из БД, иначе — из конфига
    port = (sdata or {}).get("host_port", scfg["port"])

    desc = f"**Айпи:**\n```\n{scfg['ip']}\n```\n**Порт:**\n```\n{port}\n```\n👤 Хост: {host_name}"
    if uptime:
        desc += f"\n⏱️ {uptime}"

    return discord.Embed(
        title=f"🎮 {scfg['name']} ({scfg['region_name']})", description=desc, color=discord.Color.green()
    )


class ServerInfoChoiceView(View):
    """Выбор сервера, если у пользователя/модератора их несколько."""

    def __init__(self, available_servers: list, user_id: str):
        super().__init__(timeout=60)
        self.user_id = user_id
        for server_num, sdata in available_servers:
            scfg = SERVERS[server_num]
            host_name = sdata.get("host_name", "?") if sdata else "?"
            port = (sdata or {}).get("host_port", scfg["port"])
            button = Button(
                label=f"Сервер {server_num + 1} ({scfg['ip']}:{port}) — {host_name}",
                style=discord.ButtonStyle.primary,
                custom_id=f"srv_info_choice_{server_num}",
            )
            button.callback = self._make_callback(server_num, sdata)
            self.add_item(button)

    def _make_callback(self, server_num: int, sdata: dict):
        async def callback(interaction: discord.Interaction):
            await interaction.response.edit_message(
                content=None,
                embed=build_server_info_embed(server_num, sdata),
                view=StopServerView(server_num, self.user_id),
            )

        return callback


class ServerPanelView(View):
    # Глобальный cooldown для кнопки обновления
    last_refresh_time = None
    REFRESH_COOLDOWN = 30  # секунд

    def __init__(self):
        super().__init__(timeout=None)
        # Кнопка-ссылка (справа от "Дать доступ к консоли" в ряду 1)
        self.add_item(
            Button(label="🎞️ Посмотреть реплеи", style=discord.ButtonStyle.link, url=f"{SITE_URL}/map/", row=1)
        )

    @discord.ui.button(label="🔄 Обновить", style=discord.ButtonStyle.primary, custom_id="game_refresh")
    async def refresh_button(self, interaction: discord.Interaction, button: Button):
        # Проверяем cooldown
        now = service._now()
        if ServerPanelView.last_refresh_time:
            time_since_last = (now - ServerPanelView.last_refresh_time).total_seconds()
            if time_since_last < ServerPanelView.REFRESH_COOLDOWN:
                remaining = int(ServerPanelView.REFRESH_COOLDOWN - time_since_last)
                await interaction.response.send_message(
                    f"⏳ Подождите ещё {remaining} сек. перед следующим обновлением.", ephemeral=True
                )
                return

        await interaction.response.defer()

        try:
            # Обновляем время последнего обновления
            ServerPanelView.last_refresh_time = now

            # Обновляем панель
            await update_panel_embed(interaction.client)
            await interaction.followup.send("✅ Статус обновлён!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)

    @discord.ui.button(label="⚙️ СОЗДАТЬ СЕРВЕР", style=discord.ButtonStyle.success, custom_id="game_configure")
    async def configure_button(self, interaction: discord.Interaction, button: Button):
        if all_servers_disabled():
            await interaction.response.send_message(SERVERS_UNAVAILABLE_MESSAGE, ephemeral=True)
            return

        user_id = str(interaction.user.id)
        username = interaction.user.name

        # Проверяем права
        if not check_user_has_permission(interaction):
            await interaction.response.send_message("❌ У вас нет прав для управления сервером!", ephemeral=True)
            return

        # Проверяем не запущен ли уже сервер (только для не-модераторов)
        repo = _repo(interaction.client)
        if not is_moderator(interaction) and await hosts_any_server(repo, user_id):
            await interaction.response.send_message(
                "❌ У вас уже запущен сервер! Остановите его перед запуском нового.", ephemeral=True
            )
            return

        preset = await get_user_preset(repo, user_id, interaction)

        view = await ServerSettingsView.create(repo, user_id, username, preset)
        embed = view.get_initial_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="🔒 Зарезервировать", style=discord.ButtonStyle.secondary, custom_id="game_reserve")
    async def reserve_button(self, interaction: discord.Interaction, button: Button):
        if all_servers_disabled():
            await interaction.response.send_message(SERVERS_UNAVAILABLE_MESSAGE, ephemeral=True)
            return

        user_id = str(interaction.user.id)
        username = interaction.user.name

        if not check_user_has_permission(interaction):
            await interaction.response.send_message("❌ У вас нет прав для управления сервером!", ephemeral=True)
            return

        # Проверяем нет ли уже запущенного сервера
        for scfg in SERVERS.values():
            doc = await get_current_host(_repo(interaction.client), scfg["server_id"])
            if doc and doc.get("host_id") == user_id and doc.get("host_name"):
                await interaction.response.send_message("❌ У вас уже запущен сервер!", ephemeral=True)
                return

        # Проверяем есть ли уже активная резервация у этого пользователя
        for snum in SERVERS:
            res = await get_reservation(_repo(interaction.client), snum)
            if res and res["reserved_by_id"] == user_id:
                server_name = SERVERS[snum]["name"]
                view = CancelReservationView(snum, user_id)
                await interaction.response.send_message(
                    f"🔒 У вас уже есть резервация: **{server_name}** до **{res['reserved_until'].strftime('%H:%M')} "
                    "UTC**",
                    view=view,
                    ephemeral=True,
                )
                return

        view = ReservationRegionView(user_id, username)
        await interaction.response.send_message("Выберите регион для резервации сервера:", view=view, ephemeral=True)

    @discord.ui.button(
        label="ℹ️ Информация о сервере", style=discord.ButtonStyle.primary, custom_id="game_server_info", row=2
    )
    async def server_info_button(self, interaction: discord.Interaction, button: Button):
        user_id = str(interaction.user.id)
        mod = is_moderator(interaction)

        # Собираем запущенные серверы, доступные этому пользователю
        candidates = []
        for snum, scfg in SERVERS.items():
            sdata = await get_current_host(_repo(interaction.client), scfg["server_id"])
            if sdata and sdata.get("host_name") and (mod or sdata.get("host_id") == user_id):
                candidates.append((snum, sdata))

        if not candidates:
            await interaction.response.send_message(
                "❌ У вас нет запущенного сервера." if not mod else "❌ Нет запущенных серверов.", ephemeral=True
            )
            return

        if len(candidates) == 1:
            snum, sdata = candidates[0]
            await interaction.response.send_message(
                embed=build_server_info_embed(snum, sdata), view=StopServerView(snum, user_id), ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Выберите сервер:", view=ServerInfoChoiceView(candidates, user_id), ephemeral=True
        )

    @discord.ui.button(label="🗿 Острова", style=discord.ButtonStyle.secondary, custom_id="game_islands", row=2)
    async def islands_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(embed=build_relicmod_embed(), ephemeral=True)

    @discord.ui.button(
        label="🎮 Хочу хостить (запрос доступа)", style=discord.ButtonStyle.danger, custom_id="game_want_host", row=2
    )
    async def want_host_button(self, interaction: discord.Interaction, button: Button):
        if all_servers_disabled():
            await interaction.response.send_message(SERVERS_UNAVAILABLE_MESSAGE, ephemeral=True)
            return

        # Отправляем сообщение в канал
        channel = interaction.client.get_channel(HOST_REQUEST_CHANNEL_ID)
        if channel:
            await channel.send(f"<@{HOST_REQUEST_PING_USER_ID}> {interaction.user.mention} хочет хостить сервер!")
            await interaction.response.send_message("✅ Сообщение отправлено в канал!", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Канал не найден!", ephemeral=True)

    @discord.ui.button(
        label="🎮 Открыть консоль", style=discord.ButtonStyle.success, custom_id="game_console_open", row=1
    )
    async def console_button(self, interaction: discord.Interaction, button: Button):
        from lobby_bot.features.console import cog as console_commands

        cog = _cog(interaction.client)
        running = [
            n
            for n, cfg in SERVERS.items()
            if ((await get_current_host(cog.repo, cfg["server_id"])) or {}).get("host_id")
        ]
        if not running:
            await interaction.response.send_message("❌ Нет запущенных серверов.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Выберите сервер:", view=console_commands.ConsoleServerSelectView(running, cog.console_repo), ephemeral=True
        )

    @discord.ui.button(
        label="👥 Дать доступ к консоли", style=discord.ButtonStyle.secondary, custom_id="game_console_access", row=1
    )
    async def console_access_button(self, interaction: discord.Interaction, button: Button):
        from lobby_bot.features.console import cog as console_commands

        user_id = str(interaction.user.id)
        mod = is_moderator(interaction)
        cog = _cog(interaction.client)
        hosts = {
            n: ((await get_current_host(cog.repo, cfg["server_id"])) or {}).get("host_id") for n, cfg in SERVERS.items()
        }
        eligible = [n for n, host_id in hosts.items() if host_id and (host_id == user_id or mod)]
        if not eligible:
            await interaction.response.send_message(
                "❌ Вы не хост ни одного запущенного сервера (и не модератор).", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Выберите сервер для выдачи доступа к консоли:",
            view=console_commands.ConsoleAccessServerSelectView(eligible, cog.console_repo),
            ephemeral=True,
        )


# Вспомогательная функция для остановки сервера
async def stop_server(server_num: int, interaction: discord.Interaction):
    try:
        server_config = SERVERS[server_num]
        screen_name = server_config["screen_name"]
        server_id = server_config["server_id"]

        stop_cmd = f"screen -S {screen_name} -X quit"
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, execute_ssh_command, stop_cmd, server_num)

        await _clear_host(interaction.client, server_id)

        # Обновляем панель
        if interaction:
            await update_panel_embed(interaction.client)

    except Exception as e:
        if interaction:
            await interaction.followup.send(f"❌ Ошибка при остановке: {e}", ephemeral=True)
        raise


# View для выбора сервера при остановке (с информацией о хосте)
class StopServerChoiceView(View):
    def __init__(self, available_servers: list, user_id: str):
        super().__init__(timeout=60)
        self.user_id = user_id

        for server_num, server_data in available_servers:
            server_config = SERVERS[server_num]

            # Формируем текст кнопки
            button_label = f"Сервер {server_num + 1} ({server_config['ip']}:{server_config['port']})"

            button = Button(label=button_label, style=discord.ButtonStyle.danger, custom_id=f"stop_choice_{server_num}")
            button.callback = self.create_callback(server_num, server_data)
            self.add_item(button)

    def create_callback(self, server_num: int, server_data: dict):
        async def callback(interaction: discord.Interaction):
            # Показываем подтверждение
            view = StopConfirmView(server_num, server_data, self.user_id)
            await interaction.response.edit_message(content=view.get_confirmation_text(), view=view)

        return callback


# View для подтверждения остановки
class StopConfirmView(View):
    def __init__(self, server_num: int, server_data: dict, user_id: str):
        super().__init__(timeout=60)
        self.server_num = server_num
        self.server_data = server_data
        self.user_id = user_id

    def get_confirmation_text(self):
        server_config = SERVERS[self.server_num]
        host_name = self.server_data.get("host_name", "Неизвестно") if self.server_data else "Неизвестно"
        started_at = self.server_data.get("started_at") if self.server_data else None
        uptime = get_uptime_text(started_at) if started_at else "неизвестно"

        return (
            f"**Остановить сервер?**\n\n"
            f"🎮 {server_config['name']} ({server_config['ip']}:{server_config['port']})\n"
            f"👤 Хост: {host_name}\n"
            f"⏱️ Запущен: {uptime}"
        )

    @discord.ui.button(label="✅ Да, остановить", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: Button):
        # Убираем кнопки сразу чтобы нельзя было спамить
        await interaction.response.edit_message(content="⏳ Остановка сервера...", view=None)

        server_config = SERVERS[self.server_num]

        try:
            await stop_server(self.server_num, interaction)

            # Обновляем на финальное сообщение
            await interaction.message.edit(
                content=f"✅ **Сервер остановлен**\n\n{server_config['name']} "
                f"({server_config['ip']}:{server_config['port']}) был успешно остановлен."
            )

            # Лог в канал истории
            log_channel = interaction.client.get_channel(HISTORY_CHANNEL_ID)
            if log_channel:
                log_embed = discord.Embed(title="⏹️ Сервер остановлен", color=discord.Color.red())
                log_embed.add_field(name="Сервер", value=server_config['name'], inline=False)
                log_embed.add_field(
                    name="Остановил",
                    value=f"{interaction.user.mention} ({interaction.user.display_name})",
                    inline=False,
                )
                log_embed.set_footer(text=service._now().strftime("%d.%m.%Y %H:%M UTC"))
                await log_channel.send(embed=log_embed)

        except Exception as e:
            await interaction.message.edit(content=f"❌ Ошибка при остановке: {e}")

    @discord.ui.button(label="❌ Отмена", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.edit_message(content="❌ Остановка отменена", view=None)


def build_relicmod_embed() -> discord.Embed:
    embed = discord.Embed(title="🗿 RelicMod 1.1.4 — Установка и настройка", color=discord.Color.teal())

    embed.add_field(
        name="📥 Шаг 1 — Скачать файлы",
        value=(
            "Скачать файлы для Островов и RelicMod 1.1.4: "
            f"[Google Drive]({RELICMOD_DOWNLOAD_URL}) "
            "или через **ModLauncher от Moeyy**"
        ),
        inline=False,
    )

    embed.add_field(
        name="📂 Шаг 2 — Установить файлы",
        value=f"Скинуть содержимое архива в папку с игрой:\n```\n{GAME_PAKS_PATH}\n```",
        inline=False,
    )

    embed.add_field(
        name="⚠️ Шаг 3 — Проверить ошибки",
        value="Если что-то не работает — проверьте, нет ли **старых версий RelicMod** в других папках.",
        inline=False,
    )

    embed.add_field(
        name="✅ Шаг 4 — Проверить версию",
        value="Зайдите в игру и убедитесь, что отображается версия **1.1.4**.",
        inline=False,
    )

    embed.add_field(
        name="👥 Хостинг на 11 человек",
        value=(
            "Открыть файл:\n"
            "```\nC:\\Users\\ИМЯ_УЧЕТНОЙ_ЗАПИСИ\\AppData\\Local\\Game\\Saved\\Config\\WindowsNoEditor\\Game.ini\n```"
            "Добавить в конец:\n"
            "```\n[/Script/Engine.GameSession]\nMaxPlayers=64\n```"
        ),
        inline=False,
    )

    embed.add_field(
        name="🔧 Не видишь 11 человек в лобби, а только 8?",
        value=(
            "Нужно накатить **Patcher-фикс**: "
            f"[Telegram]({TELEGRAM_CONTACT_URL}) или "
            f"[Google Drive]({PATCHER_FIX_URL})\n\n"
            "⚠️ Перед заменой файлов — **скопируйте оригиналы в отдельную папку**, чтобы можно было откатиться."
        ),
        inline=False,
    )
    return embed


# ============ ОБЩИЙ VIEW ДЛЯ ПРИНУДИТЕЛЬНОГО ЗАВЕРШЕНИЯ МАТЧА ============


async def _force_match_end(server_num: int, winner: int, interaction: discord.Interaction):
    """
    Отправляет команду force_match_end на сервер.
    winner=1 — мирные, winner=2 — предатели.
    """
    server_config = SERVERS[server_num]
    screen_name = server_config["screen_name"]
    folder = server_config["folder"]
    log_file = f"{folder}/WindowsServer/Game/Saved/Logs/Game.log"

    loop = asyncio.get_event_loop()

    log_cmd = f"grep 'Join succeeded' {log_file} || true"
    log_out, _ = await loop.run_in_executor(None, execute_ssh_command, log_cmd, server_num)

    if "Join succeeded" not in log_out:
        await interaction.followup.send("❌ В лобби нет игроков. Завершение матча невозможно.", ephemeral=True)
        return False

    cmd1 = f"screen -S {screen_name} -p 0 -X stuff 'force_match_end.js?winner={winner}'"
    cmd2 = f"screen -S {screen_name} -p 0 -X stuff $'\r'"
    await loop.run_in_executor(None, execute_ssh_command, cmd1, server_num)
    await loop.run_in_executor(None, execute_ssh_command, cmd2, server_num)
    return True


class ForceWinServerChoiceView(View):
    """Выбор сервера когда доступно несколько (для модераторов)."""

    def __init__(self, available_servers: list, user_id: str, winner: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.winner = winner

        for server_num, server_data in available_servers:
            server_config = SERVERS[server_num]
            host_name = server_data.get("host_name", "Неизвестно") if server_data else "Неизвестно"
            button_label = f"Сервер {server_num + 1} ({server_config['ip']}:{server_config['port']}) — {host_name}"

            button = Button(
                label=button_label, style=discord.ButtonStyle.primary, custom_id=f"force_win_choice_{server_num}"
            )
            button.callback = self.create_callback(server_num, server_data)
            self.add_item(button)

    def create_callback(self, server_num: int, server_data: dict):
        async def callback(interaction: discord.Interaction):
            view = ForceWinConfirmView(server_num, server_data, self.user_id, self.winner)
            await interaction.response.edit_message(content=view.get_confirmation_text(), view=view)

        return callback


class ForceWinConfirmView(View):
    """Подтверждение принудительного завершения матча."""

    def __init__(self, server_num: int, server_data: dict, user_id: str, winner: int):
        super().__init__(timeout=60)
        self.server_num = server_num
        self.server_data = server_data
        self.user_id = user_id
        self.winner = winner

    def get_confirmation_text(self):
        server_config = SERVERS[self.server_num]
        host_name = self.server_data.get("host_name", "Неизвестно") if self.server_data else "Неизвестно"
        started_at = self.server_data.get("started_at") if self.server_data else None
        uptime = get_uptime_text(started_at) if started_at else "неизвестно"
        winner_text = "🏳️ Победа мирных" if self.winner == 1 else "☠️ Победа предателей"

        return (
            f"**Завершить матч — {winner_text}?**\n\n"
            f"🎮 {server_config['name']} ({server_config['ip']}:{server_config['port']})\n"
            f"👤 Хост: {host_name}\n"
            f"⏱️ Запущен: {uptime}"
        )

    @discord.ui.button(label="✅ Да, завершить", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.edit_message(content="⏳ Завершение матча...", view=None)
        try:
            success = await _force_match_end(self.server_num, self.winner, interaction)
            if success:
                winner_text = "🏳️ Победа мирных" if self.winner == 1 else "☠️ Победа предателей"
                server_config = SERVERS[self.server_num]
                await interaction.message.edit(
                    content=f"✅ **Матч завершён — {winner_text}**\n\n"
                    f"{server_config['name']} ({server_config['ip']}:{server_config['port']})"
                )

                # Лог в канал истории
                log_channel = interaction.client.get_channel(HISTORY_CHANNEL_ID)
                if log_channel:
                    log_embed = discord.Embed(
                        title=f"🏁 Матч завершён принудительно — {winner_text}", color=discord.Color.orange()
                    )
                    log_embed.add_field(name="Сервер", value=server_config['name'], inline=False)
                    log_embed.add_field(
                        name="Завершил",
                        value=f"{interaction.user.mention} ({interaction.user.display_name})",
                        inline=False,
                    )
                    log_embed.set_footer(text=service._now().strftime("%d.%m.%Y %H:%M UTC"))
                    await log_channel.send(embed=log_embed)

        except Exception as e:
            await interaction.message.edit(content=f"❌ Ошибка: {e}")

    @discord.ui.button(label="❌ Отмена", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.edit_message(content="❌ Отменено", view=None)
