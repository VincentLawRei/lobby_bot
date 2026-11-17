import json
import shutil
from pathlib import Path

import mongomock
import pytest

from lobby_bot.features.match_stats.importer import MatchImporter
from lobby_bot.features.match_stats.repo import MatchStatsRepo
from tests.async_mongo import wrap

FIXTURES = Path(__file__).parent / "fixtures"
RANKED = "AAAA0000000000000000000000000001"
SHORT = "BBBB0000000000000000000000000002"


async def season_7() -> int:
    return 7


@pytest.fixture
def db():
    return mongomock.MongoClient().db


@pytest.fixture
def folders(tmp_path):
    incoming, processed = tmp_path / "in", tmp_path / "done"
    incoming.mkdir()
    for csv in FIXTURES.glob("*.csv"):
        shutil.copy(csv, incoming)
    return incoming, processed


@pytest.fixture
def published():
    return []


@pytest.fixture
async def importer(db, folders, published):
    repo = MatchStatsRepo(
        *(wrap(c) for c in (db.history, db.elo_queue, db.game_matches, db.ratings, db.elo_history, db.damage))
    )
    await repo.ensure_indexes()

    def publish(match_id, damage_path, positions_path, map_name):
        published.append((match_id, damage_path is not None, map_name))
        return f"https://site/map/?m={match_id}"

    return MatchImporter(repo, str(folders[0]), str(folders[1]), season=season_7, publish_map=publish)


def names(folder: Path) -> set[str]:
    return {p.name for p in folder.iterdir()} if folder.exists() else set()


def damage_file(folder: Path, match_id: str) -> None:
    (folder / f"{match_id}.json").write_text(
        json.dumps(
            {
                "match_id": match_id,
                "winner": "Traitors",
                "players": {"A": {"name": "a", "isTraitor": True}},
                "damage": {},
            }
        )
    )


async def test_ranked_match_goes_to_elo_queue(importer, db, folders):
    db.game_matches.insert_one({"guuid": RANKED, "is_ranked_mods": True})

    casual = await importer.run()

    assert casual == []
    assert db.history.count_documents({"match_uuid": RANKED, "season": 7}) == 8
    assert db.elo_queue.find_one({"match_uuid": RANKED})["processed"] is False
    assert names(folders[1]) == {f"{RANKED}.csv", f"{SHORT}.csv"}


async def test_casual_valid_match_returned_for_notification(importer, db):
    casual = await importer.run()

    assert casual == [RANKED]  # короткий матч невалиден: без уведомления
    assert db.elo_queue.count_documents({}) == 0


async def test_already_imported_match_only_moved(importer, db, folders):
    db.history.insert_one({"match_uuid": RANKED, "steam_id": "x"})

    await importer.run()

    assert db.history.count_documents({"match_uuid": RANKED}) == 1
    assert f"{RANKED}.csv" in names(folders[1])


async def test_incomplete_and_broken_files_stay_for_retry(importer, folders):
    incoming = folders[0]
    (incoming / "partial.csv").write_text("UUID\nX\n")
    (incoming / "broken.json").write_text("{nope")
    (incoming / "other.json").write_text('{"no": "match_id"}')

    await importer.run()

    assert names(incoming) == {"partial.csv", "broken.json", "other.json"}


async def test_damage_imported_once(importer, db, folders):
    damage_file(folders[0], "M1")
    await importer.run()
    damage_file(folders[0], "M1")
    await importer.run()

    assert db.damage.count_documents({"match_id": "M1"}) == 1
    assert "M1.json" in names(folders[1])


async def test_match_map_waits_for_damage(importer, db, folders, published):
    incoming = folders[0]
    (incoming / f"{RANKED}_pos.json").write_text("[]")
    await importer.run()
    assert published == [] and f"{RANKED}_pos.json" in names(incoming)

    damage_file(incoming, RANKED)
    await importer.run()

    assert published == [(RANKED, True, "harbor")]  # карта уже известна из CSV
    assert db.damage.find_one({"match_id": RANKED})["map_url"] == f"https://site/map/?m={RANKED}"
    assert f"{RANKED}_pos.json" in names(folders[1])


async def test_missing_folder_is_not_an_error(db, tmp_path):
    repo = MatchStatsRepo(
        *(wrap(c) for c in (db.history, db.elo_queue, db.game_matches, db.ratings, db.elo_history, db.damage))
    )

    assert await MatchImporter(repo, str(tmp_path / "nope"), str(tmp_path / "done"), season=season_7).run() == []
