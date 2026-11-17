from lobby_bot.features.roles.repo import RolesRepo


async def test_priority_roles_first_then_alphabetical(repo: RolesRepo):
    for role in ["Zeta", "NewPlayers", "Alpha", "Tournament"]:
        await repo.add_role(role, "x")

    assert list(await repo.get_roles()) == ["Tournament", "NewPlayers", "Alpha", "Zeta"]


async def test_host_keeps_steam_id_for_lobby_monitoring(repo: RolesRepo):
    await repo.add_role("Host", "🎮", steam_id="765")

    doc = await repo._roles.find_one({"role": "Host"})

    assert doc["emoji"] == "🎮" and doc["steam_id"] == "765"


async def test_remove_role(repo: RolesRepo):
    await repo.add_role("A", "x")
    await repo.remove_role("A")

    assert await repo.get_roles() == {}


async def test_set_description_reports_missing_role(repo: RolesRepo):
    await repo.add_role("A", "x")

    assert await repo.set_description("A", "описание") is True
    assert await repo.set_description("B", "описание") is False


async def test_roles_message_id_uses_old_state_key(repo: RolesRepo):
    """Ключ в bot_state тот же, что был в main.py: после обновления бот находит старое сообщение."""
    assert await repo.get_roles_message_id() is None
    await repo.set_roles_message_id(123)

    assert (await repo._state.find_one({"_id": "last_roles_msg_id"}))["value"] == 123
    assert await repo.get_roles_message_id() == 123
