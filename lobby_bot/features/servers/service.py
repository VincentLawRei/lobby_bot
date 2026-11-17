"""Игровые серверы: константы (серверы, карты, моды, режимы), правила и состояние серверов (без discord).

Правила — чистые функции: `is_ranked_match`, `build_game_command` (команда запуска для SSH), порты, uptime,
данные ELO-кеша для серверов. Состояние (хосты, резервы, пресеты, модификаторы) через ServersRepo:
эти функции асинхронные и первым аргументом принимают репозиторий. SSH через api.py (синхронно,
вызывающий код уносит его в поток). Поведение зафиксировано эталонами в tests/features/servers/snapshots.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

from lobby_bot.features.seasons.service import season_or_first
from lobby_bot.settings import settings

from . import api
from .api import SSH_CONFIGS  # noqa: F401  (реэкспорт для старых модулей)
from .repo import ServersRepo

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)  # в Mongo даты наивные в UTC


# Админы (Discord ID) - оставляем для обратной совместимости
ADMINS = [str(i) for i in settings.admin_ids]  # строки: сравнивается со str(user.id)

# ID ролей
MODERATOR_ROLE_ID = settings.moderator_role_id  # Может останавливать любые сервера
EXTENDED_ACCESS_ROLE_ID = settings.extended_access_role_id  # Доступ к дополнительным модам (Solo и т.д.)

# ID канала для панели управления
PANEL_CHANNEL_ID = settings.panel_channel_id

# ID канала для истории запусков серверов
HISTORY_CHANNEL_ID = settings.history_channel_id
RESERVATION_LOG_CHANNEL_ID = settings.reservation_log_channel_id

# Конфигурация серверов
SERVERS = {
    2: {
        "name": "Сервер 3",
        "folder": settings.game_server_3_folder,
        "screen_name": "sdgame",
        "port": 7777,
        "server_id": "sdgame6",
        "ip": settings.game_server_3_host,
        "region": "frankfurt",
        "region_name": "Франкфурт 3",
    },
    0: {
        "name": "Сервер 1",
        "folder": settings.game_server_1_folder,
        "screen_name": "sdgame",
        "port": 7777,
        "server_id": "sdgame4",
        "ip": settings.game_server_1_host,
        "region": "frankfurt",
        "region_name": "Франкфурт",
    },
    1: {
        "name": "Сервер 2",
        "folder": settings.game_server_2_folder,
        "screen_name": "sdgame",
        "port": 7777,
        "server_id": "sdgame5",
        "ip": settings.game_server_2_host,
        "region": "frankfurt",
        "region_name": "Франкфурт 2",
    },
}

# Серверы, отключённые вручную: DISABLED_SERVERS в .env (меняется без коммита, правкой .env и перезапуском)
DISABLED_SERVERS: set[int] = set(settings.disabled_servers)

# Сообщение, когда отключены все серверы (показывается вместо меню запуска, резервации и запроса хоста).
# Пояснение под ним (например, условия подключения) задаётся SERVERS_UNAVAILABLE_NOTE в .env
SERVERS_UNAVAILABLE_MESSAGE = "⛔ Сервера на данный момент недоступны." + (
    f"\n{settings.servers_unavailable_note}" if settings.servers_unavailable_note else ""
)

# Доступные карты
MAPS = {
    "harbor": "Station_Persistent",
    "valley": "Valley_Persistent",
    "outpost": "Outpost_Persistent",
    "islands": "Islands_Persistent",
}

MAP_NAMES = {"harbor": "Гавань", "valley": "Долина", "outpost": "Застава", "islands": "Острова"}

# Доступные моды
MODS = {
    "rand_role": "roles.js",
    # "rand_role": "rand_role.js",
    "ability_5": "5abilities.js",
    "ability_4": "4abilities.js",
    "ability_cast_start": "ability_cast_start.js",
    "ability_charge": "ability_charge_fix.js",
    "train_mode": "train_mode.js",
    "ability_upgrade": "ability_upgrade_win64.js",
    "quests": "questsinstant.js",
    "guides": "",  #
    "console": "console_win64.js",  #  Консоль
    "chill": "",  # Мод-пустышка, только для is_ranked
    "relic_mod": "",  # файлы RelicMod раскладывает build_game_command (RELIC_MOD_BASE_FILES, RELIC_MOD_MAP_FILES)
    "islands_11": "",  # Пустышка — только для maxplayers
    "islands_10": "",  # Пустышка — только для maxplayers
}

MOD_NAMES = {
    "rand_role": "Рандомные роли",
    "ability_5": "Пять способностей",
    "ability_4": "Четыре способности",
    "ability_cast_start": "Тишина на старте",
    "ability_charge": "Ability Charge",
    "train_mode": "Тренировочный мод",
    "ability_upgrade": "Ability Upgrade",
    "quests": "Квесты",
    "guides": "Советы по игре",
    "console": "Консоль",  #  Консоль
    "chill": "Chill",
    "relic_mod": "RelicMod 1.1.4",
    "islands_11": "Острова - 11 человек",
    "islands_10": "Острова - 10 человек",
}

# .js файлы RelicMod в Win64/RelicModPatches: база (всегда при релик-моде) + файлы под конкретную карту.
# build_game_command переносит нужные в Patches, остальные возвращает обратно.
RELIC_MOD_BASE_FILES = ["RelicMod_New_Role.js", "relicmodabilities33traitors.js"]
RELIC_MOD_MAP_FILES = {
    "harbor": ["RelicMod_Server_harbor_p.js"],
    "valley": ["RelicMod_Server_valley_p.js"],
    "outpost": ["RelicMod_Server_outpost_p.js"],
    "islands": ["RelicMod_Server_islands_p.js", "islands_map_fix_win64.js"],
}

# Режимы игры (доступно только для админов)
GAME_MODES = {
    "normal": {"maxplayers": 8, "traitors": 2, "name": "👥 Обычный"},
    "solo": {"maxplayers": 1, "traitors": 1, "name": "🎯 Соло"},
}

# Ranked моды - моды которые считаются "ranked"
RANKED_MODS = ["rand_role", "ability_5", "ability_4", "quests", "ability_cast_start", "guides"]

# Словарь для записи модов в БД (английские названия)
MODS_DB_NAMES = {
    "rand_role": "random_roles",
    "ability_5": "5_abilities",
    "ability_4": "4_abilities",
    "ability_cast_start": "silence_start",
    "ability_charge": "ability_charge",
    "train_mode": "train_mode",
    "ability_upgrade": "ability_upgrade",
    "quests": "quests",
    "guides": "guides",
    "chill": "chill",
    "relic_mod": "relic_mod",
    "console": "console",
    "islands_11": "islands_11",
    "islands_10": "islands_10",
}


# ID каналов для использования команд
COMMANDS_CHANNELS = settings.commands_channel_ids

# ID роли для управления серверами
HOSTER_ROLE_ID = settings.hoster_role_id


# ============ Правила ============
def is_ranked_match(mods: list, mode: str = "normal", map_choice: str | None = None) -> bool:
    """Ranked: режим normal, не Острова/Застава, без chill, все моды из RANKED_MODS (или без модов)."""
    if mode != "normal":
        return False
    if map_choice in ("islands", "outpost"):
        return False
    if "chill" in mods:
        return False
    if not mods:
        return True
    return all(mod in RANKED_MODS for mod in mods)


def get_mods_for_db(mods: list) -> list:
    """Моды в английских названиях для БД."""
    return [MODS_DB_NAMES.get(mod, mod) for mod in mods]


def all_servers_disabled() -> bool:
    """True, если запуск невозможен ни на одном сервере (все в DISABLED_SERVERS)."""
    return all(server_num in DISABLED_SERVERS for server_num in SERVERS)


def has_role(member, role_id: int) -> bool:
    """У участника сервера есть роль. member None — нет."""
    return bool(member) and any(role.id == role_id for role in member.roles)


# Дефолтные значения модификаторов
DEFAULT_MODIFIERS = {
    "maxplayers": 8,
    "daysbeforestorm": 3,
    "dayminutes": 9,
    "predatordamage": 1.0,
    "weatherintensity": 1.0,
    "staminarate": 1.0,
    "fuelburnrate": 1.0,
    "traitors": 2,
}

# Описания модификаторов для отображения
MODIFIER_DESCRIPTIONS = {
    "maxplayers": "Макс. игроков (1–12)",
    "daysbeforestorm": "Дней до бури (2–7)",
    "dayminutes": "Минут в дне (5–16)",
    "predatordamage": "Урон волков, медведей (0.25–3)",
    "weatherintensity": "Интенсивность погоды (0.25–3)",
    "staminarate": "Скорость усталости (0.25–3)",
    "fuelburnrate": "Расход топлива (0.1–2)",
    "traitors": "Количество предателей (0–8)",
}


def build_game_command(
    map_choice: str,
    mods: list,
    mode: str = "normal",
    server_num: int = 0,
    custom_modifiers: dict | None = None,
    port: int | None = None,
) -> str:
    """Собрать команду запуска с выбранными настройками"""
    # Получаем конфигурацию сервера в начале
    server_config = SERVERS[server_num]
    folder = server_config["folder"]

    map_file = MAPS[map_choice]

    # Если активны пользовательские модификаторы — берём из них, иначе из режима
    if custom_modifiers:
        maxplayers = custom_modifiers.get("maxplayers", DEFAULT_MODIFIERS["maxplayers"])
        traitors = custom_modifiers.get("traitors", DEFAULT_MODIFIERS["traitors"])
        daysbeforestorm = custom_modifiers.get("daysbeforestorm", DEFAULT_MODIFIERS["daysbeforestorm"])
        dayminutes = custom_modifiers.get("dayminutes", DEFAULT_MODIFIERS["dayminutes"])
        predatordamage = custom_modifiers.get("predatordamage", DEFAULT_MODIFIERS["predatordamage"])
        weatherintensity = custom_modifiers.get("weatherintensity", DEFAULT_MODIFIERS["weatherintensity"])
        staminarate = custom_modifiers.get("staminarate", DEFAULT_MODIFIERS["staminarate"])
        fuelburnrate = custom_modifiers.get("fuelburnrate", DEFAULT_MODIFIERS["fuelburnrate"])
    else:
        mode_params = GAME_MODES[mode]
        maxplayers = mode_params["maxplayers"]
        traitors = mode_params["traitors"]
        daysbeforestorm = DEFAULT_MODIFIERS["daysbeforestorm"]
        dayminutes = DEFAULT_MODIFIERS["dayminutes"]
        predatordamage = DEFAULT_MODIFIERS["predatordamage"]
        weatherintensity = DEFAULT_MODIFIERS["weatherintensity"]
        staminarate = DEFAULT_MODIFIERS["staminarate"]
        fuelburnrate = DEFAULT_MODIFIERS["fuelburnrate"]

    # Формируем текст с модами для announcement
    if mods:
        mods_names = [MOD_NAMES[m] for m in mods if m in MOD_NAMES]
        # Разбиваем по 3 мода на строку
        lines = []
        for i in range(0, len(mods_names), 3):
            chunk = mods_names[i : i + 3]
            lines.append(", ".join(chunk))

        # РАЗВОРАЧИВАЕМ список (игра показывает в обратном порядке)
        lines.reverse()

        # Формируем элементы с реальными переносами для perl
        mods_elements = []
        for line in lines:
            mods_elements.append(f'"{line}"')

        # Используем ,\\n для perl (реальный перенос)
        announcement_text = ',\\n    '.join(mods_elements)
    else:
        announcement_text = '""'

    # Используем perl для замены с реальным \n
    create_announcement_cmd = (
        f"perl -pe 's/_ANNOUNCEMENT_/{announcement_text}/g' {folder}/scripts/announcement_win64_template.js"
        f" > {folder}/scripts/announcement_active.js"
    )
    # Если мод Guides включен, меняем флаг прямо в сгенерированном файле[cite: 1]

    # Острова — принудительно включаем relic_mod
    effective_mods = list(mods)
    if map_choice == "islands" and "relic_mod" not in effective_mods:
        effective_mods.append("relic_mod")

    if "guides" in effective_mods:
        guides_on = "'s/var IsGuidesOn = false/var IsGuidesOn = true/g'"
        create_announcement_cmd += f" && perl -pi -e {guides_on} {folder}/scripts/announcement_active.js"

    has_relic = "relic_mod" in effective_mods

    # Острова: переопределяем maxplayers если выбран размер
    if "islands_11" in effective_mods:
        maxplayers = 11
        traitors = 3
    elif "islands_10" in effective_mods:
        maxplayers = 10
        traitors = 3

    # Моды - всегда добавляем announcement_active.js и disconnect.js; dmg_counter.js только не для Островов
    # mod_files = ["fix_modrole_win64.js"]
    # mod_files = ["announcement_active.js", "fix_modrole_win64.js", "disconnect_notice_en_win64.js"]
    mod_files = ["announcement_active.js", "disconnect_notice_en_win64.js"]
    # mod_files = ["announcement_active.js", "disconnect.js", "fix_modrole_win64.js"]
    mod_files.append("traitor_weight_win64_no_server.js")

    is_rand = "rand_role" in effective_mods
    # Command-line счётчик урона:
    #   острова            → ничего
    #   не арх + рандом      → roles_dmg_counter.js
    #   не арх + без рандома → dmg_counter.js
    if map_choice != "islands":
        if is_rand:
            mod_files.append("roles_dmg_counter.js")
        else:
            mod_files.append("dmg_counter.js")
    # if is_ranked_match(effective_mods, mode, map_choice):
    #     mod_files.append("ranked_announcement.js")

    for mod in effective_mods:
        if mod == "relic_mod":
            continue  # RelicMod файлы добавляем отдельно ниже
        if mod == "islands_11":
            continue  # Только для maxplayers, не файлы
        if mod == "islands_10":
            continue  # Только для maxplayers, не файлы
        if mod == "rand_role":
            continue  # Роли уже в roles_dmg_counter.js
        elif MODS.get(mod):  # Пропускаем пустые моды (например, chill)
            mod_files.append(MODS[mod])

    # RelicMod файлы подключаются только если patcher НЕ применяется
    # (т.е. карта не в RELIC_PATCHER_PATCHES — но сейчас все карты там, так что пропускаем)
    # Patcher сама подтянет патчи через PatchesXxx

    mods_str = " ".join(mod_files)

    # Параметры сервера уже получены выше
    screen_name = server_config["screen_name"]
    port = port if port is not None else server_config["port"]

    # ===== Подготовка файлов на диске (моментальный mv в пределах одной ФС) =====
    content_dir = f"{folder}/WindowsServer/Game/Content"
    paks_dir = f"{content_dir}/Paks"
    win64_dir = f"{folder}/WindowsServer/Game/Binaries/Win64"
    patches_dir = f"{win64_dir}/Patches"
    patches_off_dir = f"{win64_dir}/PatchesOFF"
    relic_patches_dir = f"{win64_dir}/RelicModPatches"

    # .pak/.sig островов и релик-мода — лежат в Content, при RelicMod едут в Paks
    pak_bases = ["Islands_p", "Mod_DropTable_p", "RelicMod_Server_p"]
    pak_files = [f"{b}.pak" for b in pak_bases] + [f"{b}.sig" for b in pak_bases]

    all_relic_js = RELIC_MOD_BASE_FILES + [f for files in RELIC_MOD_MAP_FILES.values() for f in files]

    def _mv_if(src: str, dst: str) -> str:
        # Перемещает файл только если он существует; всегда возвращает код 0
        return f'if [ -e "{src}" ]; then mv -f "{src}" "{dst}"; fi'

    fs_cmds = [f'mkdir -p "{paks_dir}" "{patches_dir}" "{patches_off_dir}" "{relic_patches_dir}"']

    # 1) .pak/.sig — при RelicMod (острова его форсит) в Paks, иначе обратно в Content
    for f in pak_files:
        if has_relic:
            fs_cmds.append(_mv_if(f'{content_dir}/{f}', f'{paks_dir}/{f}'))
        else:
            fs_cmds.append(_mv_if(f'{paks_dir}/{f}', f'{content_dir}/{f}'))

    # 2) Roles.js — при рандомных ролях в Patches, иначе в PatchesOFF
    if is_rand:
        fs_cmds.append(_mv_if(f'{patches_off_dir}/Roles.js', f'{patches_dir}/Roles.js'))
    else:
        fs_cmds.append(_mv_if(f'{patches_dir}/Roles.js', f'{patches_off_dir}/Roles.js'))

    # 3) RelicMod .js — сброс всех обратно, затем раскладка нужных под текущую карту
    for f in all_relic_js:
        fs_cmds.append(_mv_if(f'{patches_dir}/{f}', f'{relic_patches_dir}/{f}'))
    if has_relic:
        for f in RELIC_MOD_BASE_FILES + RELIC_MOD_MAP_FILES.get(map_choice, []):
            fs_cmds.append(_mv_if(f'{relic_patches_dir}/{f}', f'{patches_dir}/{f}'))

    fs_setup = " && ".join(fs_cmds)

    server_exe = "WindowsServer/Game/Binaries/Win64/GameServer-Win64-Shipping.exe"
    options = (
        f"?maxplayers={maxplayers}?daysbeforestorm={daysbeforestorm}?dayminutes={dayminutes}"
        f"?predatordamage={predatordamage}?weatherintensity={weatherintensity}?staminarate={staminarate}"
        f"?fuelburnrate={fuelburnrate}?traitors={traitors}?port={port}"
    )
    return (
        f'{fs_setup} && {create_announcement_cmd} && cd {folder} && screen -dmS {screen_name} '
        f'wine runner.exe "{server_exe}" {map_file}{options} {mods_str}'
    )


AVAILABLE_PORTS = [7777, 7778, 7779]


def port_rotation(current_port) -> tuple[int, int]:
    """(порт для этого запуска, следующий порт): 7777 -> 7778 -> 7779 -> 7777."""
    if current_port not in AVAILABLE_PORTS:
        current_port = 7777
    return current_port, AVAILABLE_PORTS[(AVAILABLE_PORTS.index(current_port) + 1) % len(AVAILABLE_PORTS)]


def uptime_text(started_at: datetime | None, now: datetime) -> str | None:
    """Сколько прошло с запуска: «2 дн. 3 ч. назад», «3 ч. 5 мин. назад», «5 мин. назад»."""
    if not started_at:
        return None
    delta = now - started_at
    hours = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60
    if delta.days > 0:
        return f"{delta.days} дн. {hours} ч. назад"
    if hours > 0:
        return f"{hours} ч. {minutes} мин. назад"
    return f"{minutes} мин. назад"


def parse_guuid(log_line: str) -> str | None:
    """GUUID матча из строки лога «...LogGameState: GUUID=22EF...»."""
    if not log_line:
        return None
    parts = log_line.strip().split("GUUID=")
    return parts[1].strip() if len(parts) == 2 else None


def server_num_by_id(server_id: str) -> int | None:
    return next((n for n, cfg in SERVERS.items() if cfg["server_id"] == server_id), None)


# ============ SSH ============
def server_running(server_num: int) -> bool:
    """Запущен ли сервер (есть screen). Отключённый или недоступный — False."""
    if server_num in DISABLED_SERVERS:
        return False
    try:
        screen_name = SERVERS[server_num]["screen_name"]
        stdout, _ = api.execute_ssh_command(f"screen -list | grep {screen_name} || true", server_num)
        return screen_name in stdout
    except Exception as e:
        logger.warning(f"⚠️ Сервер {server_num} недоступен: {e}")
        return False


def match_guuid(server_num: int) -> str | None:
    """GUUID последнего матча из лога сервера."""
    log_file = f"{SERVERS[server_num]['folder']}/WindowsServer/Game/Saved/Logs/Game.log"
    stdout, _ = api.execute_ssh_command(f"grep 'LogGameState: GUUID=' {log_file} | tail -1", server_num)
    return parse_guuid(stdout)


# ============ Пресеты и модификаторы ============
async def get_user_preset(repo: ServersRepo, user_id: str, has_extended: bool) -> dict:
    """Пресет запуска пользователя. Без расширенного доступа режим всегда normal."""
    preset = await repo.preset(user_id)
    if not preset:
        return {
            "map": "harbor",
            "mods": ["rand_role", "ability_cast_start"],
            "mode": "normal",
            "region": "frankfurt",
            "is_admin": has_extended,
        }
    saved_mode = preset.get("mode", "normal")
    if not has_extended and saved_mode != "normal":
        saved_mode = "normal"
    return {
        "map": preset.get("map", "harbor"),
        "mods": preset.get("mods", ["rand_role", "ability_cast_start"]),
        "mode": saved_mode,
        "region": preset.get("region", "frankfurt"),
        "is_admin": has_extended,
    }


async def save_user_preset(
    repo: ServersRepo, user_id: str, map_choice: str, mods: list, mode: str = "normal", region: str = "frankfurt"
) -> None:
    await repo.set_preset_fields(
        user_id, {"map": map_choice, "mods": mods, "mode": mode, "region": region, "updated_at": _now()}
    )


async def get_user_modifiers(repo: ServersRepo, user_id: str) -> dict | None:
    """Пользовательские модификаторы; None, если не настроены или выключены."""
    doc = await repo.preset(user_id)
    if not doc:
        return None
    modifiers = doc.get("custom_modifiers")
    if not modifiers or not doc.get("custom_modifiers_enabled", False):
        return None
    return modifiers


async def save_user_modifiers(repo: ServersRepo, user_id: str, modifiers: dict) -> None:
    """Сохранить модификаторы (статус включения не меняется)."""
    await repo.set_preset_fields(user_id, {"custom_modifiers": modifiers, "updated_at": _now()})


async def set_user_modifiers_enabled(repo: ServersRepo, user_id: str, enabled: bool) -> None:
    await repo.set_preset_fields(user_id, {"custom_modifiers_enabled": enabled})


async def get_user_modifiers_raw(repo: ServersRepo, user_id: str) -> tuple[dict | None, bool]:
    """Модификаторы и статус включения (даже если выключены)."""
    doc = await repo.preset(user_id)
    if not doc:
        return None, False
    return doc.get("custom_modifiers"), doc.get("custom_modifiers_enabled", False)


# ============ Хосты, порты, резервы ============
async def get_next_port(repo: ServersRepo, server_id: str) -> int:
    """Порт для этого запуска; следующий по ротации сохраняется."""
    doc = await repo.state(server_id)
    port, next_port = port_rotation(doc.get("current_port", 7777) if doc else 7777)
    await repo.set_state(server_id, {"current_port": next_port})
    return port


async def get_current_host(repo: ServersRepo, server_id: str) -> dict | None:
    """Состояние сервера: хост, время запуска, порт, резерв."""
    return await repo.state(server_id) or None


async def set_current_host(
    repo: ServersRepo, server_id: str, user_id: str, username: str, port: int | None = None
) -> None:
    fields = {"host_id": user_id, "host_name": username, "started_at": _now()}
    if port is not None:
        fields["host_port"] = port
    await repo.set_state(server_id, fields)


async def clear_current_host(repo: ServersRepo, console_repo, server_id: str) -> None:
    """Очистить хоста; заодно снимается доступ к консоли, выданный на этот матч (console_repo: ConsoleRepo)."""
    await repo.set_state(server_id, {"host_id": None, "host_name": None, "started_at": None})
    server_num = server_num_by_id(server_id)
    if server_num is not None:
        from lobby_bot.features.console.service import revoke_console_access  # фичи общаются через service

        await revoke_console_access(console_repo, server_num)


async def reserve_server(repo: ServersRepo, server_num: int, user_id: str, username: str) -> datetime:
    """Резерв сервера на 10 минут. Возвращает время окончания."""
    reserved_until = _now().replace(microsecond=0) + timedelta(minutes=10)
    await repo.set_state(
        SERVERS[server_num]["server_id"],
        {"reserved_by_id": user_id, "reserved_by_name": username, "reserved_until": reserved_until},
    )
    return reserved_until


async def get_reservation(repo: ServersRepo, server_num: int) -> dict | None:
    """Активный резерв; None, если нет или истёк (чистит reservation_cleanup_task)."""
    doc = await repo.state(SERVERS[server_num]["server_id"])
    if not doc or not doc.get("reserved_until"):
        return None
    if _now() > doc["reserved_until"]:
        return None
    return {
        "reserved_by_id": doc.get("reserved_by_id"),
        "reserved_by_name": doc.get("reserved_by_name"),
        "reserved_until": doc["reserved_until"],
    }


async def clear_reservation(repo: ServersRepo, server_num: int) -> None:
    await repo.unset_state(SERVERS[server_num]["server_id"], ["reserved_by_id", "reserved_by_name", "reserved_until"])


async def is_server_occupied(repo: ServersRepo, server_num: int, for_user_id: str | None = None) -> bool:
    """Занят ли сервер резервом другого пользователя."""
    reservation = await get_reservation(repo, server_num)
    return bool(reservation and reservation["reserved_by_id"] != for_user_id)


def get_uptime_text(started_at) -> str | None:
    return uptime_text(started_at, _now())


# ============ Матчи и панель ============
async def save_match_info(
    repo: ServersRepo, guuid: str, mods: list, mode: str = "normal", map_choice: str | None = None
) -> None:
    is_ranked = is_ranked_match(mods, mode, map_choice)
    mods_db = get_mods_for_db(mods)
    await repo.save_match(guuid, is_ranked, mods_db, _now())
    logger.debug(f"DEBUG: Сохранен матч GUUID={guuid}, ranked={is_ranked}, mods={mods_db}")


async def get_panel_message_id(repo: ServersRepo) -> int | None:
    return await repo.panel_message_id()


async def save_panel_message_id(repo: ServersRepo, message_id: int) -> None:
    await repo.save_panel_message_id(message_id)


# ============ ELO-кеш для игровых серверов ============
TRAITOR_BASE_CHANCE = 0.25  # ожидаемая доля игр предателем
TRAITOR_WEIGHT_BONUS = 25  # поправка веса на предателя, если предателем чаще/реже ожидаемого
NEW_PLAYER_WEIGHT = -85  # без игр: реже на предателя


def build_elo_cache(ratings: list[dict]) -> dict:
    """steam_id -> рейтинг и статистика для скриптов сервера (+ место rank по ELO)."""
    cache = {}
    for r in ratings:
        steam_id = r.get("steam_id")
        if not steam_id:
            continue
        cache[steam_id] = {
            "elo": r.get("elo", 1500),
            "wins": r.get("wins", 0),
            "losses": r.get("losses", 0),
            "total_games": r.get("total_games", 0),
            "traitors_games": r.get("traitors_games", 0),
            "traitors_wins": r.get("traitors_wins", 0),
            "crew_games": r.get("crew_games", 0),
            "crew_wins": r.get("crew_wins", 0),
            "role_stats": r.get("role_stats", {}),
            "crew_winstreak": r.get("crew_winstreak", 0),
            "traitor_winstreak": r.get("traitor_winstreak", 0),
            "is_calibrated": r.get("is_calibrated", False),
            "calibration_games": r.get("calibration_games", 0),
            "current_name": r.get("changed_nickname") or r.get("current_name", "UNKNOWN"),
        }
    for i, sid in enumerate(sorted(cache, key=lambda x: cache[x]["elo"], reverse=True)):
        cache[sid]["rank"] = i + 1
    return cache


def traitor_weights(cache: dict, overrides: dict[str, int]) -> list[list]:
    """[[steam_id, вес]] для выбора предателей: доля игр предателем относительно ожидаемой + ручные бонусы."""
    result = []
    for steam_id, data in cache.items():
        total = data.get("total_games", 0)
        traitors = data.get("traitors_games", 0)
        weight = 0
        if total < 1:
            weight = NEW_PLAYER_WEIGHT
        else:
            chance = traitors / total
            if chance < TRAITOR_BASE_CHANCE:
                weight = TRAITOR_WEIGHT_BONUS
            elif chance > TRAITOR_BASE_CHANCE:
                weight = -TRAITOR_WEIGHT_BONUS
        weight += overrides.get(steam_id, 0)
        if weight != 0:
            result.append([steam_id, weight])
    for steam_id, bonus in overrides.items():  # ручные бонусы игроков без рейтинга
        if steam_id not in cache:
            result.append([steam_id, bonus])
    return result


def traitor_pair_winrates(rows: list[dict]) -> dict:
    """Для каждого игрока — напарники на предателях (только пары по 2 предателя) с играми и винрейтом.

    Лучшие напарники первыми."""
    matches = {}
    for doc in rows:
        matches.setdefault(doc.get("match_uuid"), []).append(doc)

    pair_stats = {}
    for players in matches.values():
        if len(players) != 2:
            continue
        p1, p2 = players
        id1, id2 = p1["steam_id"], p2["steam_id"]
        if id1 == id2:
            continue
        stats = pair_stats.setdefault(tuple(sorted([id1, id2])), {"games": 0, "wins": 0, "names": {}})
        stats["games"] += 1
        stats["names"][id1] = p1.get("name", id1)
        stats["names"][id2] = p2.get("name", id2)
        if p1.get("won"):
            stats["wins"] += 1

    result = {}
    for (id1, id2), stats in pair_stats.items():
        games, wins = stats["games"], stats["wins"]
        winrate = round(wins / games * 100, 1) if games else 0.0
        for me, partner in ((id1, id2), (id2, id1)):
            result.setdefault(me, []).append(
                {
                    "partner_steam_id": partner,
                    "partner_name": stats["names"].get(partner, partner),
                    "games": games,
                    "wins": wins,
                    "winrate": winrate,
                }
            )
    for steam_id in result:
        result[steam_id].sort(key=lambda x: (x["winrate"], x["games"]), reverse=True)
    return result


async def generate_elo_cache(repo: ServersRepo, elo_repo, seasons, server_num: int) -> bool:
    """elo_cache.json и traitor_weight JS на сервер. False при любой ошибке (пишется в лог).
    Данные читаются асинхронно (ручные бонусы предателей у фичи elo, elo_repo: EloRepo; сезон у seasons,
    seasons: SeasonsRepo), загрузка по SSH в потоке."""
    from lobby_bot.features.elo.service import elo_overrides  # фичи общаются через service

    try:
        season = await season_or_first(seasons)
        cache = build_elo_cache(await repo.season_ratings(season))
        logger.info(f"[ELO CACHE] Generated for {len(cache)} players")
        overrides = await elo_overrides(elo_repo)
        await asyncio.to_thread(_upload_elo_cache, cache, overrides, server_num)
        return True
    except Exception as e:
        logger.exception(f"[ELO CACHE] Error: {e}")
        return False


def _upload_elo_cache(cache: dict, overrides: dict[str, int], server_num: int) -> None:
    remote_path = f"{SERVERS[server_num]['folder']}/scripts/elo_cache.json"
    api.upload_json(server_num, cache, remote_path)
    logger.info(f"[ELO CACHE] Uploaded to server {server_num}: {remote_path}")
    generate_traitor_weight_js(cache, overrides, server_num)


def generate_traitor_weight_js(cache: dict, overrides: dict[str, int], server_num: int) -> bool:
    """traitor_weight_win64_no_server.js из шаблона на сервере (AdditionalScore). False при ошибке."""
    weights = traitor_weights(cache, overrides)
    try:
        folder = SERVERS[server_num]["folder"]
        api.patch_remote_template(
            server_num,
            f"{folder}/scripts/traitor_weight_template.js",
            f"{folder}/scripts/traitor_weight_win64_no_server.js",
            "ADDITIONAL_SCORE",
            json.dumps(weights, indent=4),
        )
        logger.info(f"[TRAITOR WEIGHT] Updated for server {server_num}: {len(weights)} players")
        return True
    except Exception as e:
        logger.exception(f"[TRAITOR WEIGHT] Error: {e}")
        return False


async def calculate_traitor_pair_winrates(repo: ServersRepo) -> dict:
    return traitor_pair_winrates(await repo.ranked_traitor_rows())


async def generate_traitor_best_winrates(repo: ServersRepo, server_num: int) -> bool:
    """traitor_best_winrates.json на сервер. False при ошибке."""
    try:
        cache = await calculate_traitor_pair_winrates(repo)
        logger.info(f"[TRAITOR WINRATES] Generated for {len(cache)} players")
        remote_path = f"{SERVERS[server_num]['folder']}/scripts/traitor_best_winrates.json"
        await asyncio.to_thread(api.upload_json, server_num, cache, remote_path)
        logger.info(f"[TRAITOR WINRATES] Uploaded to server {server_num}: {remote_path}")
        return True
    except Exception as e:
        logger.exception(f"[TRAITOR WINRATES] Error: {e}")
        return False
