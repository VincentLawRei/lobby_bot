"""Консоль игровых серверов: команды в консоль (по SSH-туннелю), доступ, точки телепорта (без discord).

Функции с базой асинхронные, первым аргументом принимают ConsoleRepo. Поведение зафиксировано эталонами
в tests/features/console/snapshots.
"""

import json
import logging
from datetime import UTC, datetime

import paramiko

from lobby_bot.features.servers.service import ADMINS, MODERATOR_ROLE_ID, SERVERS, SSH_CONFIGS  # noqa: F401
from lobby_bot.settings import settings

from .repo import GLOBAL_KEY, ConsoleRepo

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


CONSOLE_PORT = 16021
COORD_LIMIT = 50000  # координата должна быть в диапазоне [-COORD_LIMIT, COORD_LIMIT]
MAX_LOCATIONS = 15  # максимум сохранённых точек на пользователя
SHARE_CHANNEL_ID = settings.share_channel_id  # канал, куда публикуются координаты для шаринга

# Восстановление: свойства "буста" (тепло / еда / здоровье) и значение по умолчанию
RESTORE_PROPS = ["WarmthBoostRemaining", "StaminaBoostRemaining", "HealthBoostRemaining"]
RESTORE_VALUE = 1000.0

# Каталог выдаваемых предметов по категориям: категория -> {label: путь к классу}.
# Добавлять новый предмет — дописать строку в нужную категорию.
# Путь: /Game/Blueprints/Inventory/<Папка>/<Файл>.<Файл>_C
ITEM_CATEGORIES = {
    "Оружие / инструменты": {
        "Топор (Wood Axe)": "/Game/Blueprints/Inventory/WoodAxe/BP_WoodAxe_Inventory.BP_WoodAxe_Inventory_C",
        "Кирка (Pickaxe)": "/Game/Blueprints/Inventory/Pickaxe/BP_Pickaxe_Inventory.BP_Pickaxe_Inventory_C",
        "Сабля (Sword)": "/Game/Blueprints/Inventory/Sword/BP_Sword_Inventory.BP_Sword_Inventory_C",
        "Тесак (Cleaver)": "/Game/Blueprints/Inventory/Cleaver/BP_Cleaver_Inventory.BP_Cleaver_Inventory_C",
        "Лопата (Shovel)": "/Game/Blueprints/Inventory/Shovel/BP_Shovel_Inventory.BP_Shovel_Inventory_C",
        "Лук (Bow)": "/Game/Blueprints/Inventory/Bow/BP_Bow_Inventory.BP_Bow_Inventory_C",
        "Стрелы (Arrows)": "/Game/Blueprints/Inventory/Bow/BP_Arrows_Inventory.BP_Arrows_Inventory_C",
        "Старый пистолет (Pistol)": ("/Game/Blueprints/Inventory/Pistol/BP_Pistol_Inventory.BP_Pistol_Inventory_C"),
        "Патроны (Pistol Ammo)": (
            "/Game/Blueprints/Inventory/Pistol/BP_Pistol_Ammo_Inventory.BP_Pistol_Ammo_Inventory_C"
        ),
        "Порох (Gunpowder)": "/Game/Blueprints/Inventory/Pistol/BP_Gunpowder_Inventory.BP_Gunpowder_Inventory_C",
        "Детали ружья (GunParts)": "/Game/Blueprints/Inventory/Pistol/BP_GunParts_Inventory.BP_GunParts_Inventory_C",
        "Медвежий капкан (BearTrap)": (
            "/Game/Blueprints/Inventory/BearTrap/BP_BearTrap_Inventory.BP_BearTrap_Inventory_C"
        ),
        "Винтовка (Rifle)": "/Game/Blueprints/Inventory/Rifle/BP_Rifle_Inventory.BP_Rifle_Inventory_C",
    },
    "Еда": {
        "Мясо животного (Animal Meat)": (
            "/Game/Blueprints/Inventory/Meat/BP_AnimalMeat_Inventory.BP_AnimalMeat_Inventory_C"
        ),
        "Странное мясо (Strange Meat)": (
            "/Game/Blueprints/Inventory/Meat/BP_StrangeMeat_Inventory.BP_StrangeMeat_Inventory_C"
        ),
        "Приготовленное мясо (Cooked Meat)": (
            "/Game/Blueprints/Inventory/Meat/BP_CookedMeat_Inventory.BP_CookedMeat_Inventory_C"
        ),
        "Чай (Tea)": "/Game/Blueprints/Inventory/Tea/BP_Tea_Inventory.BP_Tea_Inventory_C",
    },
    "Ресурсы / прочее": {
        "Заряд (Charge)": "/Game/Blueprints/Environment/Charge/BP_Charge_Inventory.BP_Charge_Inventory_C",
        "Пороховая бочка (Powderkeg)": (
            "/Game/Blueprints/Inventory/Powderkeg/BP_Powderkeg_Inventory.BP_Powderkeg_Inventory_C"
        ),
        "Шприц (Syringe)": "/Game/Blueprints/Inventory/Syringe/BP_Syringe_Inventory.BP_Syringe_Inventory_C",
        "Кость (Club)": "/Game/Blueprints/Inventory/Meat/BP_Club_Inventory.BP_Club_Inventory_C",
        "Камень (Rock)": "/Game/Blueprints/Inventory/Rock/BP_Rock_Inventory.BP_Rock_Inventory_C",
        "Палка (Stick)": "/Game/Blueprints/Inventory/Stick/BP_Stick_Inventory.BP_Stick_Inventory_C",
        "Топливная бочка (FuelBarrel)": (
            "/Game/Blueprints/Inventory/Powderkeg/BP_FuelBarrel_Inventory.BP_FuelBarrel_Inventory_C"
        ),
        "Топливо (Fuel)": "/Game/Blueprints/Inventory/Fuel/BP_Fuel_Inventory.BP_Fuel_Inventory_C",
        "Гвозди (Nails)": "/Game/Blueprints/Inventory/Metals/BP_Nails_Inventory.BP_Nails_Inventory_C",
        "Слиток железа (IronIngot)": (
            "/Game/Blueprints/Inventory/Metals/BP_IronIngot_Inventory.BP_IronIngot_Inventory_C"
        ),
        "Фонарь (Lantern)": "/Game/Blueprints/Inventory/Lantern/BP_Lantern_Inventory.BP_Lantern_Inventory_C",
        "Яд (Poison)": "/Game/Blueprints/Inventory/Poison/BP_Poison_Inventory.BP_Poison_Inventory_C",
        "Противоядие (Antidote)": "/Game/Blueprints/Inventory/Poison/BP_Antidote_Inventory.BP_Antidote_Inventory_C",
    },
}

# Плоский словарь label -> путь (для выдачи и обратной совместимости)
ITEMS = {label: path for cat in ITEM_CATEGORIES.values() for label, path in cat.items()}

# Пресеты координат для телепорта: label -> "x,y,z".
# Добавлять новые локации — просто дописать строку сюда.
LOCATIONS = {
    "Скала с зарядом #1": "5153,9200,9200",
    "Скала с зарядом #2": "6000,8692,8293",
    "Скала с зарядом #3": "4977,8827,8326",
}


# ============ Команда в консоль ============
def send_console_command(server_num: int, cmd: dict) -> tuple[bool, str]:
    """JSON-команда в консоль сервера через SSH-туннель (direct-tcpip), без открытия портов наружу.

    (True, "OK") или (False, текст ошибки). Синхронная: из async вызывать в потоке.
    """
    if server_num not in SSH_CONFIGS:
        return False, "Неизвестный сервер"
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(**SSH_CONFIGS[server_num], timeout=5, banner_timeout=5, auth_timeout=5)
        channel = ssh.get_transport().open_channel(
            "direct-tcpip", dest_addr=("localhost", CONSOLE_PORT), src_addr=("localhost", 0), timeout=5
        )
        channel.send(json.dumps(cmd).encode("utf-16-le"))
        channel.close()
        return True, "OK"
    except Exception as e:
        return False, str(e)
    finally:
        ssh.close()


# ============ Доступ ============
def is_moderator(user) -> bool:
    """Модератор консоли: роль MODERATOR_ROLE_ID или ID из ADMIN_IDS."""
    return any(r.id == MODERATOR_ROLE_ID for r in user.roles) or str(user.id) in ADMINS


async def has_console_access(repo: ConsoleRepo, user, server_num: int) -> bool:
    """Модератор, админ или выданный хостом доступ к этому серверу."""
    if is_moderator(user):
        return True
    return str(user.id) in await repo.access_user_ids(server_num)


async def grant_console_access(repo: ConsoleRepo, server_num: int, user_ids: list[str], granted_by: str) -> None:
    await repo.grant_access(server_num, user_ids, granted_by, _now())


async def revoke_console_access(repo: ConsoleRepo, server_num: int) -> None:
    """Вызывается при остановке/завершении сервера (servers.clear_current_host)."""
    await repo.revoke_access(server_num)


# ============ Координаты ============
def fmt_coords(x: float, y: float, z: float) -> str:
    """Координаты всегда целые (дробные округляются)."""
    return f"{round(x)},{round(y)},{round(z)}"


def coords_out_of_range(location_str: str) -> bool:
    """True, если хотя бы одна координата вне [-COORD_LIMIT, COORD_LIMIT] или строка битая."""
    try:
        parts = [float(v) for v in location_str.split(",")]
    except ValueError:
        return True
    if len(parts) != 3:
        return True
    return any(v > COORD_LIMIT or v < -COORD_LIMIT for v in parts)


# ============ Точки пользователя и глобальные ============
async def get_user_locations(repo: ConsoleRepo, user_id) -> dict:
    return await repo.locations(str(user_id))


async def add_user_location(repo: ConsoleRepo, user_id, name: str, coords: str) -> tuple[bool, str]:
    """Добавить/перезаписать точку. Перезапись существующего имени лимит не трогает."""
    current = await repo.locations(str(user_id))
    if name not in current and len(current) >= MAX_LOCATIONS:
        return False, (
            f"Достигнут лимит {MAX_LOCATIONS} точек. Удалите ненужную или введите название "
            f"существующей, чтобы заменить её."
        )
    await repo.put_location(str(user_id), name, coords, _now())
    return True, "OK"


async def remove_user_location(repo: ConsoleRepo, user_id, name: str) -> None:
    await repo.remove_location(str(user_id), name)


async def set_last_coords(repo: ConsoleRepo, user_id, coords: str) -> None:
    await repo.set_last_coords(str(user_id), coords)


async def get_last_coords(repo: ConsoleRepo, user_id) -> str | None:
    return await repo.last_coords(str(user_id))


async def get_global_db_locations(repo: ConsoleRepo) -> dict:
    """Глобальные точки из БД (без пресетов из кода). Их можно удалять."""
    return await repo.locations(GLOBAL_KEY)


async def get_global_locations(repo: ConsoleRepo) -> dict:
    """Пресеты LOCATIONS + точки из БД (БД перекрывает по имени)."""
    merged = dict(LOCATIONS)
    merged.update(await get_global_db_locations(repo))
    return merged


async def add_global_location(repo: ConsoleRepo, name: str, coords: str) -> None:
    await repo.put_location(GLOBAL_KEY, name, coords, _now())


async def remove_global_location(repo: ConsoleRepo, name: str) -> None:
    await repo.remove_location(GLOBAL_KEY, name)
