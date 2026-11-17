from datetime import UTC, datetime, timedelta

from lobby_bot.features.lobbies.cog import (
    board_text,
    lobby_block,
    needs_new_notification,
    notification_text,
)

from .conftest import make_lobby

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)  # 15:00 по Москве


# ---------- Текст ----------
async def test_lobby_block():
    lobby = make_lobby("Crab", "S1", "D1", 6, 2, True, age=30)

    text = lobby_block(lobby, "Краб", "Crab", ["Crab", "Бобёр"], 1, NOW)

    assert text == (
        "----------------------\n👤 **Crab** (Краб) [RU] (6 / 8)\n----------------------\n"
        "В лобби: :ballot_box_with_check:\nСоздано: 30 мин назад (14:30)\n"
        "Игроки в лобби:\n`Crab, Бобёр, ???`\n\n"
    )


async def test_lobby_block_without_known_players():
    text = lobby_block(make_lobby("Crab", "S1", "D1", 1, 7, False), "Crab", "", [], 3, NOW)

    assert "👤 **Crab** [RU] (1 / 8)" in text and "В лобби: :x:" in text
    assert text.endswith("`Ничего не найдено`\n\n")


async def test_board_text_empty_and_limited():
    assert "Не найдено ни одного лобби" in board_text([], NOW)
    assert "_Обновлено: 01-10-2026 15:00:00 (GMT +3)_" in board_text([], NOW)
    assert len(board_text(["x" * 3000], NOW)) == 2000


async def test_notification_text():
    lobby = make_lobby("Crab", "S1", "D1", 6, 2, False)

    assert (
        notification_text("<@&H>", "<@&All>", lobby, "крабы", started=True)
        == "<@&H> +2 \n\n<@&All>\n||\n**Описание:** крабы||\nУже запустились в игру: да"
    )
    assert notification_text("<@&H>", "<@&All>", lobby, "  ", started=False) == "<@&H> +2 \n\n<@&All>\n"


async def test_new_notification_not_more_often_than_45_min():
    assert needs_new_notification(None, NOW)
    assert needs_new_notification("broken", NOW)
    assert not needs_new_notification((NOW - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S%z"), NOW)
    assert needs_new_notification((NOW - timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M:%S%z"), NOW)


# ---------- Цикл мониторинга ----------
async def test_board_message_created_then_edited(cog, api, channels):
    api.lobbies = [make_lobby("Crab", "S1", "D1", 6, 2, True, members=[("D1", "S1", "Crab")], public=["D1"])]

    await cog.update()
    await cog.update()

    assert channels["board"].log == [("send", 1), ("edit", 1)]
    assert "👤 **Crab** [RU] (6 / 8)" in channels["board"].messages[1].content


async def test_players_saved_and_full_lobby_snapshotted(cog, api, mongo):
    api.lobbies = [
        make_lobby(
            "Crab", "S1", "D1", 8, 0, False, members=[("D1", "S1", "Crab"), ("D2", "S2", "Bob")], public=["D1", "D2"]
        )
    ]

    await cog.update()

    assert {p["other_id"] for p in mongo.players.find()} == {"S1", "S2"}
    assert mongo.lobbies.find_one()["players"] == ["D1", "D2"]


async def test_host_notification_lifecycle(cog, api, channels, mongo, repo):
    mongo.roles_and_emojis.insert_one({"role": "CrabHost", "emoji": "🦀", "steam_id": "S1"})
    notify = channels["notify"]

    api.lobbies = [make_lobby("Crab", "S1", "D1", 6, 2, True)]
    await cog.update()  # хост открыл лобби: сообщение с пингом
    await cog.update()  # меньше 45 минут: правка, а не новое сообщение
    api.lobbies = [make_lobby("Crab", "S1", "D1", 8, 0, False)]
    await cog.update()  # игра началась: пометка в том же сообщении
    api.lobbies = []
    await cog.update()  # лобби пропало: уведомление удаляется

    assert notify.log == [("send", 1), ("edit", 1), ("edit", 1), ("delete", 1)]
    assert await repo.get_notify_state("CrabHost") == (None, None)


async def test_api_error_still_updates_board(cog, api, channels):
    async def broken():
        raise RuntimeError("EOS down")

    api.get_lobbies = broken

    await cog.update()

    assert "Не найдено ни одного лобби" in channels["board"].messages[1].content
