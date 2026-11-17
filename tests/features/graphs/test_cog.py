import json
from datetime import UTC, datetime, timedelta

import mongomock

from lobby_bot.features.graphs.cog import GraphsCog
from lobby_bot.features.graphs.repo import GraphsRepo
from lobby_bot.features.seasons.repo import SeasonsRepo
from tests.async_mongo import wrap


async def test_publish_uploads_both_graphs():
    db = mongomock.MongoClient().db
    db.seasons.insert_one({"season": 1, "is_active": True})
    now = datetime.now(UTC).replace(tzinfo=None)
    db.players.insert_many(
        [{"other_id": f"S{i}", "game_uid": f"D{i}", "nickname": f"P{i}", "date": now} for i in range(3)]
    )
    db.lobbies.insert_many(
        [{"players": ["D0", "D1", "D2"], "host": "D0", "timestamp": now - timedelta(hours=1)} for _ in range(6)]
    )
    uploads = {}
    repo = GraphsRepo(
        wrap(db.lobbies),
        wrap(db.players),
        wrap(db.history_player_stats),
        wrap(db.player_ratings),
        SeasonsRepo(wrap(db.seasons), wrap(db.elo_processing)),
    )
    cog = GraphsCog(bot=None, repo=repo, upload=lambda path, data: uploads.update({path: data}))

    await cog.publish_full_graph()
    await cog.publish_daily_graph()

    assert set(uploads) == {"players_graph.html", "players_graph_daily.html"}
    assert b'"label": "P0"' in uploads["players_graph.html"]
    assert json.dumps("хостил 6 раз(а)")[1:-1].encode() in uploads["players_graph_daily.html"]
