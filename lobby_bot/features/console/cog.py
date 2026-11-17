"""Консоль игровых серверов в Discord: выбор сервера и роли, MinHealth, телепорт (свои, сохранённые и
глобальные точки), выдача предметов, восстановление, броадкаст, выдача доступа хостом, шаринг точек в канал.

Своих команд нет: открывается кнопками панели серверов (ConsoleServerSelectView, ConsoleAccessServerSelectView).
Данные и отправка команд в service.py. Репозиторий ConsoleRepo передаётся во views, кнопки и модальные окна
явно; views, которым при создании нужны данные, создаются фабрикой `create()`. Кнопка шаринга точки
persistent (DynamicItem, регистрируется в setup): после рестарта берёт репозиторий у ConsoleCog.
"""

import asyncio

import discord
from discord.ext import commands
from discord.ui import Button, Modal, Select, TextInput, UserSelect, View

from .repo import ConsoleRepo
from .service import (
    COORD_LIMIT,
    ITEM_CATEGORIES,
    ITEMS,
    MAX_LOCATIONS,
    RESTORE_PROPS,
    RESTORE_VALUE,
    SERVERS,
    SHARE_CHANNEL_ID,
    add_global_location,
    add_user_location,
    coords_out_of_range,
    fmt_coords,
    get_global_db_locations,
    get_global_locations,
    get_last_coords,
    get_user_locations,
    grant_console_access,
    has_console_access,
    is_moderator,
    remove_global_location,
    remove_user_location,
    send_console_command,
    set_last_coords,
)


async def _send(server_num: int, cmd: dict) -> tuple[bool, str]:
    """Команда в консоль в потоке: SSH не блокирует бота."""
    return await asyncio.to_thread(send_console_command, server_num, cmd)


# ---------- UI: выдача доступа (хост/модератор) ----------


class ConsoleAccessSelect(UserSelect):
    def __init__(self, server_num: int, repo: ConsoleRepo):
        self.server_num = server_num
        self.repo = repo
        super().__init__(placeholder="Кому дать доступ к консоли", min_values=0, max_values=25)

    async def callback(self, interaction: discord.Interaction):
        user_ids = [str(u.id) for u in self.values]
        await grant_console_access(self.repo, self.server_num, user_ids, str(interaction.user.id))
        mentions = ", ".join(u.mention for u in self.values) if self.values else "никто (доступ очищен)"
        await interaction.response.send_message(f"✅ Доступ к консоли: {mentions}", ephemeral=True)


class ConsoleAccessView(View):
    def __init__(self, server_num: int, repo: ConsoleRepo):
        super().__init__(timeout=180)
        self.add_item(ConsoleAccessSelect(server_num, repo))


class ConsoleAccessServerSelect(Select):
    """Выбор сервера, для которого хост/модератор выдаёт доступ к консоли."""

    def __init__(self, server_nums: list, repo: ConsoleRepo):
        self.repo = repo
        options = [discord.SelectOption(label=SERVERS[n]["name"], value=str(n)) for n in server_nums]
        super().__init__(placeholder="Выберите сервер", options=options)

    async def callback(self, interaction: discord.Interaction):
        server_num = int(self.values[0])
        await interaction.response.edit_message(
            content=f"Выдача доступа к консоли — {SERVERS[server_num]['name']}:",
            view=ConsoleAccessView(server_num, self.repo),
        )


class ConsoleAccessServerSelectView(View):
    def __init__(self, server_nums: list, repo: ConsoleRepo):
        super().__init__(timeout=120)
        self.add_item(ConsoleAccessServerSelect(server_nums, repo))


# ---------- UI: MinHealth ----------


class MinHealthCustomModal(Modal, title="MinHealth — своё значение"):
    value = TextInput(label="Значение (0.0 - 100.0)", placeholder="напр. 25.5", required=True, max_length=10)

    def __init__(self, server_num: int, role: str):
        super().__init__()
        self.server_num = server_num
        self.role = role

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = float(self.value.value)
        except ValueError:
            await interaction.response.send_message("❌ Некорректное число.", ephemeral=True)
            return

        cmd = {"op": "sethuman", "role": self.role, "propname": "MinHealth", "value": val}
        ok, msg = await _send(self.server_num, cmd)
        text = f"✅ {self.role}: MinHealth = {val}" if ok else f"❌ Ошибка: {msg}"
        await interaction.response.edit_message(content=text)


# ---------- UI: Телепортация ----------


async def _do_teleport(
    interaction: discord.Interaction, repo: ConsoleRepo, server_num: int, role: str, location_str: str
):
    if coords_out_of_range(location_str):
        await interaction.response.edit_message(
            content=f"❌ Координаты вне допустимого диапазона (от -{COORD_LIMIT} до {COORD_LIMIT}). Телепорт отменён."
        )
        return
    cmd = {"op": "TeleportPlayerToLocation", "role": role, "location": location_str}
    ok, msg = await _send(server_num, cmd)
    if ok:
        await set_last_coords(repo, interaction.user.id, location_str)
    text = f"✅ {role} телепортирован в ({location_str})" if ok else f"❌ Ошибка: {msg}"
    await interaction.response.edit_message(content=text)


class TeleportModal(Modal, title="Телепортация — координаты"):
    x = TextInput(label="X", required=True)
    y = TextInput(label="Y", required=True)
    z = TextInput(label="Z", required=True)

    def __init__(self, server_num: int, role: str, repo: ConsoleRepo):
        super().__init__()
        self.server_num = server_num
        self.role = role
        self.repo = repo

    async def on_submit(self, interaction: discord.Interaction):
        try:
            x, y, z = float(self.x.value), float(self.y.value), float(self.z.value)
        except ValueError:
            await interaction.response.send_message("❌ Координаты должны быть числами.", ephemeral=True)
            return
        await _do_teleport(interaction, self.repo, self.server_num, self.role, fmt_coords(x, y, z))


class LocationPresetSelect(Select):
    def __init__(self, server_num: int, role: str, repo: ConsoleRepo, locations: dict):
        self.server_num = server_num
        self.role = role
        self.repo = repo
        options = [
            discord.SelectOption(label=lbl, description=coords, value=lbl)
            for lbl, coords in list(locations.items())[:25]
        ]
        super().__init__(placeholder="Выберите локацию", options=options)

    async def callback(self, interaction: discord.Interaction):
        coords = (await get_global_locations(self.repo)).get(self.values[0])
        if not coords:
            await interaction.response.send_message("❌ Локация не найдена.", ephemeral=True)
            return
        await _do_teleport(interaction, self.repo, self.server_num, self.role, coords)


class TeleportCustomButton(Button):
    def __init__(self, server_num: int, role: str, repo: ConsoleRepo):
        super().__init__(label="Свои координаты", style=discord.ButtonStyle.primary)
        self.server_num = server_num
        self.role = role
        self.repo = repo

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TeleportModal(self.server_num, self.role, self.repo))


class RepeatTeleportButton(Button):
    """Повторяет телепорт по последним координатам — обходит проблему селекта
    (повторный выбор той же опции Discord не регистрирует)."""

    def __init__(self, server_num: int, role: str, repo: ConsoleRepo):
        super().__init__(label="🔁 Повторить последний", style=discord.ButtonStyle.secondary)
        self.server_num = server_num
        self.role = role
        self.repo = repo

    async def callback(self, interaction: discord.Interaction):
        last = await get_last_coords(self.repo, interaction.user.id)
        if not last:
            await interaction.response.send_message("❌ Нет последнего телепорта.", ephemeral=True)
            return
        await _do_teleport(interaction, self.repo, self.server_num, self.role, last)


class TeleportView(View):
    def __init__(self, server_num: int, role: str, repo: ConsoleRepo, global_locations: dict):
        super().__init__(timeout=120)
        if global_locations:
            self.add_item(LocationPresetSelect(server_num, role, repo, global_locations))
        self.add_item(TeleportCustomButton(server_num, role, repo))
        self.add_item(RepeatTeleportButton(server_num, role, repo))
        self.add_item(MyLocationsButton(server_num, role, repo))

    @classmethod
    async def create(cls, server_num: int, role: str, repo: ConsoleRepo) -> "TeleportView":
        return cls(server_num, role, repo, await get_global_locations(repo))


# ---------- UI: мои точки ----------


class AddLocationModal(Modal, title="Сохранить координаты"):
    name = TextInput(label="Название", placeholder="напр. Скала с зарядом", required=True, max_length=50)
    x = TextInput(label="X", required=True)
    y = TextInput(label="Y", required=True)
    z = TextInput(label="Z", required=True)

    def __init__(self, server_num: int, role: str, repo: ConsoleRepo):
        super().__init__()
        self.server_num = server_num
        self.role = role
        self.repo = repo

    async def on_submit(self, interaction: discord.Interaction):
        try:
            x, y, z = float(self.x.value), float(self.y.value), float(self.z.value)
        except ValueError:
            await interaction.response.send_message("❌ Координаты должны быть числами.", ephemeral=True)
            return
        coords = fmt_coords(x, y, z)
        if coords_out_of_range(coords):
            await interaction.response.send_message(
                f"❌ Координаты вне диапазона (от -{COORD_LIMIT} до {COORD_LIMIT}). Не сохранено.", ephemeral=True
            )
            return
        ok, reason = await add_user_location(self.repo, interaction.user.id, self.name.value.strip(), coords)
        if not ok:
            await interaction.response.send_message(f"❌ {reason}", ephemeral=True)
            return
        view = await MyLocationsView.create(
            self.server_num, self.role, interaction.user.id, self.repo, is_mod=is_moderator(interaction.user)
        )
        await interaction.response.edit_message(
            content=f"✅ Сохранено: **{self.name.value.strip()}** ({coords})\n\n{view.render_text()}", view=view
        )


class SaveLastModal(Modal, title="Сохранить последнюю точку"):
    name = TextInput(label="Название", placeholder="напр. Скала с зарядом", required=True, max_length=50)

    def __init__(self, coords: str, server_num: int, role: str, repo: ConsoleRepo):
        super().__init__()
        self.coords = coords
        self.server_num = server_num
        self.role = role
        self.repo = repo

    async def on_submit(self, interaction: discord.Interaction):
        ok, reason = await add_user_location(self.repo, interaction.user.id, self.name.value.strip(), self.coords)
        if not ok:
            await interaction.response.send_message(f"❌ {reason}", ephemeral=True)
            return
        view = await MyLocationsView.create(
            self.server_num, self.role, interaction.user.id, self.repo, is_mod=is_moderator(interaction.user)
        )
        await interaction.response.edit_message(
            content=f"✅ Сохранено: **{self.name.value.strip()}** ({self.coords})\n\n{view.render_text()}", view=view
        )


class MyLocationSelect(Select):
    """Телепорт по одной из сохранённых локаций пользователя."""

    def __init__(self, server_num: int, role: str, locations: dict, repo: ConsoleRepo):
        self.server_num = server_num
        self.role = role
        self.repo = repo
        options = [
            discord.SelectOption(label=name, description=coords, value=name) for name, coords in locations.items()
        ]
        super().__init__(placeholder="Телепорт в сохранённую локацию", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        locations = await get_user_locations(self.repo, interaction.user.id)
        coords = locations.get(self.values[0])
        if not coords:
            await interaction.response.send_message("❌ Локация не найдена (возможно, удалена).", ephemeral=True)
            return
        await _do_teleport(interaction, self.repo, self.server_num, self.role, coords)


class MyLocationDeleteSelect(Select):
    def __init__(self, locations: dict):
        options = [
            discord.SelectOption(label=name, description=coords, value=name) for name, coords in locations.items()
        ]
        super().__init__(placeholder="Удалить локацию", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        name = self.values[0]
        repo = self.view.repo
        await remove_user_location(repo, interaction.user.id, name)
        # Перерисовываем панель с обновлённым списком
        view = await MyLocationsView.create(
            self.view.server_num, self.view.role, interaction.user.id, repo, is_mod=is_moderator(interaction.user)
        )
        await interaction.response.edit_message(content=view.render_text(), view=view)


class AddLocationButton(Button):
    def __init__(self, server_num: int, role: str, repo: ConsoleRepo):
        super().__init__(label="➕ Добавить", style=discord.ButtonStyle.success, row=2)
        self.server_num = server_num
        self.role = role
        self.repo = repo

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddLocationModal(self.server_num, self.role, self.repo))


class SaveLastButton(Button):
    def __init__(self, server_num: int, role: str, repo: ConsoleRepo):
        super().__init__(label="💾 Сохранить последнюю", style=discord.ButtonStyle.primary, row=2)
        self.server_num = server_num
        self.role = role
        self.repo = repo

    async def callback(self, interaction: discord.Interaction):
        last = await get_last_coords(self.repo, interaction.user.id)
        if not last:
            await interaction.response.send_message(
                "❌ Нет последних координат. Сначала телепортируйтесь по своим координатам.", ephemeral=True
            )
            return
        await interaction.response.send_modal(SaveLastModal(last, self.server_num, self.role, self.repo))


# ---------- Шаринг координат в канал ----------
# Кнопка persistent (DynamicItem): координаты закодированы в custom_id,
# поэтому работает и после перезапуска бота. Регистрируется в setup() этой фичи.


class ShareLocationButton(discord.ui.DynamicItem[Button], template=r"shareloc:(?P<name>.+)\|(?P<coords>[-\d,]+)"):
    def __init__(self, name: str, coords: str, repo: ConsoleRepo):
        self.loc_name = name
        self.coords = coords
        self.repo = repo
        safe_name = name.replace("|", "/")
        super().__init__(
            Button(
                label=f"➕ Добавить себе: {safe_name}"[:80],
                style=discord.ButtonStyle.success,
                custom_id=f"shareloc:{safe_name}|{coords}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["name"], match["coords"], interaction.client.get_cog(ConsoleCog.__cog_name__).repo)

    async def callback(self, interaction: discord.Interaction):
        ok, reason = await add_user_location(self.repo, interaction.user.id, self.loc_name, self.coords)
        if not ok:
            await interaction.response.send_message(f"❌ {reason}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ Добавлено в ваши точки: **{self.loc_name}** ({self.coords})", ephemeral=True
        )


async def post_shared_location(client, repo: ConsoleRepo, name: str, coords: str, author: discord.Member) -> bool:
    channel = client.get_channel(SHARE_CHANNEL_ID)
    if channel is None:
        return False
    view = View(timeout=None)
    view.add_item(ShareLocationButton(name, coords, repo))
    await channel.send(
        content=(
            f"📍 **{author.display_name}** делится координатами: "
            f"**{name}** ({coords})\nНажмите кнопку, чтобы добавить в свои сохранённые."
        ),
        view=view,
    )
    return True


class ShareLocationSelect(Select):
    def __init__(self, locations: dict):
        options = [
            discord.SelectOption(label=name, description=coords, value=name) for name, coords in locations.items()
        ]
        super().__init__(placeholder="📤 Поделиться локацией", options=options, row=3)

    async def callback(self, interaction: discord.Interaction):
        name = self.values[0]
        repo = self.view.repo
        coords = (await get_user_locations(repo, interaction.user.id)).get(name)
        if not coords:
            await interaction.response.edit_message(content="❌ Локация не найдена.")
            return
        ok = await post_shared_location(interaction.client, repo, name, coords, interaction.user)
        text = f"✅ Опубликовано в канал: **{name}** ({coords})" if ok else "❌ Канал для шаринга не найден."
        await interaction.response.edit_message(content=text, view=self.view)


class MyLocationsView(View):
    def __init__(
        self, server_num: int, role: str, user_id: str, repo: ConsoleRepo, locations: dict, is_mod: bool = False
    ):
        super().__init__(timeout=180)
        self.server_num = server_num
        self.role = role
        self.user_id = user_id
        self.repo = repo
        self.locations = locations
        if locations:
            self.add_item(MyLocationSelect(server_num, role, locations, repo))
            self.add_item(MyLocationDeleteSelect(locations))
            self.add_item(ShareLocationSelect(locations))
        self.add_item(AddLocationButton(server_num, role, repo))
        self.add_item(SaveLastButton(server_num, role, repo))
        if is_mod:
            self.add_item(GlobalManageButton(server_num, role, repo))

    @classmethod
    async def create(
        cls, server_num: int, role: str, user_id: str, repo: ConsoleRepo, is_mod: bool = False
    ) -> "MyLocationsView":
        return cls(server_num, role, user_id, repo, await get_user_locations(repo, user_id), is_mod=is_mod)

    def render_text(self) -> str:
        count = len(self.locations)
        return (
            f"⭐ **Мои сохранённые локации** ({count}/{MAX_LOCATIONS})\n"
            f"Выберите для телепорта, удалите ненужную, добавьте новую "
            f"или сохраните последнюю использованную точку."
        )


class MyLocationsButton(Button):
    def __init__(self, server_num: int, role: str, repo: ConsoleRepo):
        super().__init__(label="⭐ Мои координаты", style=discord.ButtonStyle.secondary)
        self.server_num = server_num
        self.role = role
        self.repo = repo

    async def callback(self, interaction: discord.Interaction):
        view = await MyLocationsView.create(
            self.server_num, self.role, interaction.user.id, self.repo, is_mod=is_moderator(interaction.user)
        )
        await interaction.response.send_message(content=view.render_text(), view=view, ephemeral=True)


# ---------- UI: управление глобальными точками (только модераторы) ----------


class AddGlobalModal(Modal, title="Глобальная точка"):
    name = TextInput(label="Название", placeholder="напр. Скала с зарядом", required=True, max_length=50)
    x = TextInput(label="X", required=True)
    y = TextInput(label="Y", required=True)
    z = TextInput(label="Z", required=True)

    def __init__(self, server_num: int, role: str, repo: ConsoleRepo):
        super().__init__()
        self.server_num = server_num
        self.role = role
        self.repo = repo

    async def on_submit(self, interaction: discord.Interaction):
        try:
            x, y, z = float(self.x.value), float(self.y.value), float(self.z.value)
        except ValueError:
            await interaction.response.send_message("❌ Координаты должны быть числами.", ephemeral=True)
            return
        coords = fmt_coords(x, y, z)
        if coords_out_of_range(coords):
            await interaction.response.send_message(
                f"❌ Координаты вне диапазона (от -{COORD_LIMIT} до {COORD_LIMIT}). Не сохранено.", ephemeral=True
            )
            return
        await add_global_location(self.repo, self.name.value.strip(), coords)
        view = await GlobalManageView.create(self.server_num, self.role, self.repo)
        await interaction.response.edit_message(
            content=f"✅ Глобальная точка сохранена: **{self.name.value.strip()}** ({coords})\n\n{view.render_text()}",
            view=view,
        )


class GlobalDeleteSelect(Select):
    """Удаление только БД-точек. Хардкод-пресеты LOCATIONS удалять нельзя (они в коде)."""

    def __init__(self, db_locations: dict):
        options = [
            discord.SelectOption(label=name, description=coords, value=name) for name, coords in db_locations.items()
        ]
        super().__init__(placeholder="Удалить глобальную точку", options=options)

    async def callback(self, interaction: discord.Interaction):
        if not is_moderator(interaction.user):
            await interaction.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return
        repo = self.view.repo
        await remove_global_location(repo, self.values[0])
        view = await GlobalManageView.create(self.view.server_num, self.view.role, repo)
        await interaction.response.edit_message(content=view.render_text(), view=view)


class AddGlobalButton(Button):
    def __init__(self, server_num: int, role: str, repo: ConsoleRepo):
        super().__init__(label="➕ Добавить глобальную", style=discord.ButtonStyle.success)
        self.server_num = server_num
        self.role = role
        self.repo = repo

    async def callback(self, interaction: discord.Interaction):
        if not is_moderator(interaction.user):
            await interaction.response.send_message("❌ Недостаточно прав.", ephemeral=True)
            return
        await interaction.response.send_modal(AddGlobalModal(self.server_num, self.role, self.repo))


class GlobalManageView(View):
    def __init__(self, server_num: int, role: str, repo: ConsoleRepo, db_locations: dict, total: int):
        super().__init__(timeout=180)
        self.server_num = server_num
        self.role = role
        self.repo = repo
        self.db_locations = db_locations
        self.total = total
        if db_locations:
            self.add_item(GlobalDeleteSelect(db_locations))
        self.add_item(AddGlobalButton(server_num, role, repo))

    @classmethod
    async def create(cls, server_num: int, role: str, repo: ConsoleRepo) -> "GlobalManageView":
        return cls(server_num, role, repo, await get_global_db_locations(repo), len(await get_global_locations(repo)))

    def render_text(self) -> str:
        total = self.total
        db_count = len(self.db_locations)
        return (
            f"🌐 **Глобальные точки** (всего {total}, из них в БД {db_count})\n"
            f"Видны всем игрокам в списке телепорта. Базовые пресеты из кода удалять нельзя."
        )


class GlobalManageButton(Button):
    def __init__(self, server_num: int, role: str, repo: ConsoleRepo):
        super().__init__(label="🌐 Глобальные (модер)", style=discord.ButtonStyle.secondary)
        self.server_num = server_num
        self.role = role
        self.repo = repo

    async def callback(self, interaction: discord.Interaction):
        if not is_moderator(interaction.user):
            await interaction.response.send_message("❌ Только модераторы/админы.", ephemeral=True)
            return
        view = await GlobalManageView.create(self.server_num, self.role, self.repo)
        await interaction.response.send_message(content=view.render_text(), view=view, ephemeral=True)


# ---------- UI: Выдача предметов ----------


class ItemCountSelect(Select):
    def __init__(self, current: int = 1):
        options = [discord.SelectOption(label=str(i), value=str(i), default=(i == current)) for i in range(1, 11)]
        super().__init__(placeholder=f"Количество: {current}", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_count = int(self.values[0])
        self.view.rebuild()
        await interaction.response.edit_message(view=self.view)


class ItemCategorySelect(Select):
    def __init__(self, current: str | None):
        options = [discord.SelectOption(label=cat, value=cat, default=(cat == current)) for cat in ITEM_CATEGORIES]
        super().__init__(placeholder="Категория" if not current else f"Категория: {current}", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_category = self.values[0]
        self.view.selected_item = None  # сбрасываем предмет при смене категории
        self.view.rebuild()
        await interaction.response.edit_message(view=self.view)


class ItemSelect(Select):
    def __init__(self, category: str, current: str | None):
        items = ITEM_CATEGORIES[category]
        options = [discord.SelectOption(label=label, value=label, default=(label == current)) for label in items]
        super().__init__(
            placeholder="Выберите предмет" if not current else f"Предмет: {current}", options=options, row=2
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_item = self.values[0]
        self.view.rebuild()
        await interaction.response.edit_message(view=self.view)


class GiveItemView(View):
    def __init__(self, server_num: int, role: str):
        super().__init__(timeout=120)
        self.server_num = server_num
        self.role = role
        self.selected_category = next(iter(ITEM_CATEGORIES))  # первая категория по умолчанию
        self.selected_item = None
        self.selected_count = 1
        self.rebuild()

    def rebuild(self):
        """Пересобирает селекты с учётом текущих выборов (обновляет плейсхолдеры/список предметов)."""
        self.clear_items()
        self.add_item(ItemCountSelect(self.selected_count))
        self.add_item(ItemCategorySelect(self.selected_category))
        self.add_item(ItemSelect(self.selected_category, self.selected_item))
        self.add_item(self.give_btn)

    @discord.ui.button(label="Выдать предметы", style=discord.ButtonStyle.success, row=3)
    async def give_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_item:
            await interaction.response.send_message("❌ Сначала выберите предмет.", ephemeral=True)
            return
        path = ITEMS[self.selected_item]
        cmd = {"op": "additem", "role": self.role, "itemname": path, "count": self.selected_count}
        ok, msg = await _send(self.server_num, cmd)
        text = f"✅ {self.role}: выдано {self.selected_item} × {self.selected_count}" if ok else f"❌ Ошибка: {msg}"
        await interaction.response.edit_message(content=text)


# Совместимость со старым именем, используемым в кнопке панели
ItemSelectView = GiveItemView


# ---------- UI: Броадкаст (сообщение всем) ----------


class BroadcastModal(Modal, title="Сообщение всем игрокам"):
    msg = TextInput(
        label="Текст",
        placeholder="Сообщение для всего сервера",
        required=True,
        max_length=200,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, server_num: int):
        super().__init__()
        self.server_num = server_num

    async def on_submit(self, interaction: discord.Interaction):
        ok, msg = await _send(self.server_num, {"op": "broadcast", "msg": self.msg.value})
        text = "✅ Сообщение отправлено всем игрокам." if ok else f"❌ Ошибка: {msg}"
        await interaction.response.edit_message(content=text)


# ---------- UI: выбор роли + кнопки команд ----------
class ConsoleTargetSelect(Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Scout (Разведчица)", value="Scout"),
            discord.SelectOption(label="Commander (Командир)", value="Commander"),
            discord.SelectOption(label="Priest (Священник)", value="Priest"),
            discord.SelectOption(label="Medic (Медик)", value="Medic"),
            discord.SelectOption(label="Mechanic (Механик)", value="Mechanic"),
            discord.SelectOption(label="Quartermaster (Интендант)", value="Quartermaster"),
            discord.SelectOption(label="Guard (Охранник)", value="Guard"),
            discord.SelectOption(label="Pilot (Пилот)", value="Pilot"),
        ]
        super().__init__(placeholder="Выберите роль (кому)", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        selected_option = next(o for o in self.options if o.value == self.values[0])
        self.view.selected_target = self.values[0]
        self.placeholder = f"Цель: {selected_option.label}"
        await self.view.update_message(interaction)


class ServerConsoleControlView(View):
    def __init__(self, server_num: int, repo: ConsoleRepo):
        super().__init__(timeout=300)
        self.server_num = server_num
        self.repo = repo
        self.selected_target = None
        self.add_item(ConsoleTargetSelect())

    async def update_message(self, interaction: discord.Interaction):
        status_text = f"🎮 **Консоль: {SERVERS[self.server_num]['name']}**"
        await interaction.response.edit_message(content=status_text, view=self)

    def _require_role(self):
        return self.selected_target

    @discord.ui.button(label="MinHealth", style=discord.ButtonStyle.primary, row=1)
    async def minhealth_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._require_role():
            await interaction.response.send_message("❌ Сначала выберите роль.", ephemeral=True)
            return
        await interaction.response.send_modal(MinHealthCustomModal(self.server_num, self.selected_target))

    @discord.ui.button(label="Телепорт", style=discord.ButtonStyle.primary, row=1)
    async def teleport_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._require_role():
            await interaction.response.send_message("❌ Сначала выберите роль.", ephemeral=True)
            return
        await interaction.response.send_message(
            content=f"Телепорт — {self.selected_target}:",
            view=await TeleportView.create(self.server_num, self.selected_target, self.repo),
            ephemeral=True,
        )

    @discord.ui.button(label="Выдать предмет", style=discord.ButtonStyle.success, row=1)
    async def giveitem_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._require_role():
            await interaction.response.send_message("❌ Сначала выберите роль.", ephemeral=True)
            return
        await interaction.response.send_message(
            content=f"Выдача предмета — {self.selected_target}:",
            view=ItemSelectView(self.server_num, self.selected_target),
            ephemeral=True,
        )

    @discord.ui.button(label="🩹 Восстановить", style=discord.ButtonStyle.success, row=1)
    async def restore_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._require_role():
            await interaction.response.send_message("❌ Сначала выберите роль.", ephemeral=True)
            return
        errors = []
        for prop in RESTORE_PROPS:
            ok, msg = await _send(
                self.server_num,
                {"op": "sethuman", "role": self.selected_target, "propname": prop, "value": RESTORE_VALUE},
            )
            if not ok:
                errors.append(f"{prop}: {msg}")
        if errors:
            text = "❌ Ошибки:\n" + "\n".join(errors)
        else:
            text = f"✅ {self.selected_target}: тепло, еда и здоровье восстановлены (+{int(RESTORE_VALUE)})."
        await interaction.response.edit_message(content=text)

    @discord.ui.button(label="📢 Броадкаст", style=discord.ButtonStyle.secondary, row=2)
    async def broadcast_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Роль не нужна — сообщение отправляется всем игрокам сервера
        await interaction.response.send_modal(BroadcastModal(self.server_num))


# ---------- Точка входа: Кнопка "Консоль" в панели ----------


class ConsoleServerSelect(Select):
    """Выбор запущенного сервера при входе в консоль."""

    def __init__(self, server_nums: list, repo: ConsoleRepo):
        self.repo = repo
        options = [discord.SelectOption(label=SERVERS[n]["name"], value=str(n)) for n in server_nums]
        super().__init__(placeholder="Выберите сервер для управления", options=options)

    async def callback(self, interaction: discord.Interaction):
        server_num = int(self.values[0])
        if not await has_console_access(self.repo, interaction.user, server_num):
            await interaction.response.send_message("❌ У вас нет доступа к консоли этого сервера.", ephemeral=True)
            return

        await interaction.response.edit_message(
            content=f"🎮 **Консоль: {SERVERS[server_num]['name']}**",
            view=ServerConsoleControlView(server_num, self.repo),
        )


class ConsoleServerSelectView(View):
    def __init__(self, server_nums: list, repo: ConsoleRepo):
        super().__init__(timeout=120)
        self.add_item(ConsoleServerSelect(server_nums, repo))


class ConsoleCog(commands.Cog):
    """Своих команд и задач нет; Cog подключает фичу и хранит репозиторий для persistent-кнопки шаринга."""

    def __init__(self, bot: commands.Bot, repo: ConsoleRepo) -> None:
        self.bot = bot
        self.repo = repo


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(ShareLocationButton)  # persistent: кнопки под старыми сообщениями работают после рестарта
    await bot.add_cog(ConsoleCog(bot, ConsoleRepo(bot.db.console_access, bot.db.player_locations)))
