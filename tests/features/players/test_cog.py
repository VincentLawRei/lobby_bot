from datetime import datetime

import mongomock
import pytest

from lobby_bot.features.players.cog import PlayersCog
from lobby_bot.features.players.repo import PlayersRepo
from lobby_bot.shared.permissions import NotModerator
from tests.async_mongo import wrap
from tests.fakes import FakeContext, user


@pytest.fixture
def col():
    col = mongomock.MongoClient().db.players
    col.insert_many(
        [
            {"nickname": "CrabKing", "game_uid": "game1", "other_id": "765", "date": datetime(2026, 1, 2)},
            {"nickname": "crab_alt", "game_uid": "game2", "other_id": "765", "date": datetime(2026, 1, 3)},
            {"nickname": "Other", "game_uid": "game3", "other_id": "999"},
        ]
    )
    return col


@pytest.fixture
def cog(col):
    return PlayersCog(bot=None, repo=PlayersRepo(wrap(col)))


async def test_only_moderators(cog):
    with pytest.raises(NotModerator):
        cog.cog_check(FakeContext(author=user(10)))


async def test_search_case_insensitive_newest_first(cog):
    ctx = FakeContext()
    await cog.search.callback(cog, ctx, nickname_part="CRAB")

    text = ctx.replies[0]
    assert text.index("crab_alt") < text.index("CrabKing")
    assert "**Последняя дата:** 2026-01-03 00:00" in text
    assert "Other" not in text


async def test_search_nothing_found(cog):
    ctx = FakeContext()
    await cog.search.callback(cog, ctx, nickname_part="zzz")
    assert ctx.replies == ["Ничего не найдено."]


async def test_search_splits_long_output(cog, col):
    col.insert_many([{"nickname": f"crab{i}", "other_id": str(i)} for i in range(30)])
    ctx = FakeContext()
    await cog.search.callback(cog, ctx, nickname_part="crab")
    assert len(ctx.replies) > 1 and all(len(r) < 2000 for r in ctx.replies)


async def test_nick_by_game_uid_updates_all_records(cog, col):
    ctx = FakeContext()
    await cog.nick.callback(cog, ctx, "game1", changednickname="Краб")

    assert ctx.replies == ["✅ Обновлено **2** записей для steam_id `765`.\nНовый ник: **Краб**"]
    assert {d.get("changed_nickname") for d in col.find({"other_id": "765"})} == {"Краб"}


async def test_twitch_by_steam_id_and_unchanged(cog):
    ctx = FakeContext()
    await cog.twitch.callback(cog, ctx, "999", twitch_username="streamer")
    await cog.twitch.callback(cog, ctx, "999", twitch_username="streamer")

    assert ctx.replies[0].endswith("Twitch: **streamer**")
    assert ctx.replies[1] == "⚠️ Записи найдены, но Twitch уже был `streamer`."


async def test_unknown_player(cog):
    ctx = FakeContext()
    await cog.nick.callback(cog, ctx, "nope", changednickname="x")
    assert ctx.replies == ["❌ Игрок с ID `nope` не найден."]
