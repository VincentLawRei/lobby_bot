from datetime import datetime, timedelta

from .conftest import make_lobby

NOW = datetime(2026, 10, 1, 12, 0)


async def test_upsert_player_counts_appearances(repo, mongo):
    await repo.upsert_player("Crab", "D1", "S1", NOW)
    await repo.upsert_player("CrabNew", "D1", "S1", NOW)

    doc = mongo.players.find_one({"other_id": "S1"})
    assert (doc["nickname"], doc["counter"], doc["changed_nickname"]) == ("CrabNew", 2, None)


async def test_public_player_names_prefer_moderator_nick(repo, mongo):
    mongo.players.insert_many(
        [
            {"game_uid": "D1", "nickname": "old", "changed_nickname": None, "date": NOW - timedelta(days=1)},
            {"game_uid": "D1", "nickname": "new", "changed_nickname": None, "date": NOW},
            {"game_uid": "D2", "nickname": "bob", "changed_nickname": "Бобёр", "date": NOW},
        ]
    )

    names, missing = await repo.public_player_names(["D1", "D2", "D9"])

    assert sorted(names) == ["new", "Бобёр"] and missing == 1


async def test_snapshot_only_russian_and_not_more_often_than_30_min(repo, mongo):
    ru = make_lobby("Crab", "S1", "D1", 8, 0, False, public=["D1"])
    await repo.save_snapshot(make_lobby("En", "S2", "D2", 8, 0, False, language="EN", session="s2"), NOW)
    await repo.save_snapshot(ru, NOW)
    await repo.save_snapshot(ru, NOW + timedelta(minutes=10))
    await repo.save_snapshot(ru, NOW + timedelta(minutes=31))

    assert mongo.lobbies.count_documents({}) == 2


async def test_notify_state(repo):
    assert await repo.get_notify_state("CrabHost") == (None, None)
    await repo.set_notify_state("CrabHost", 5, "2026-10-01 12:00:00+0000")

    assert await repo.get_notify_state("CrabHost") == (5, "2026-10-01 12:00:00+0000")
    assert await repo.notified_roles() == ["CrabHost"]
