"""Сезоны: текущий сезон всегда читается из базы, /reset_season применяется без перезапуска бота."""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import mongomock
import pytest

from lobby_bot.features.seasons.cog import SeasonsCog
from lobby_bot.features.seasons.repo import SeasonsRepo
from lobby_bot.features.seasons.service import current_season, start_new_season
from lobby_bot.settings import settings
from tests import mongo as db
from tests.async_mongo import wrap

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 1)


@pytest.fixture
def clean_db():
    """Общая база в памяти (db.py): очищаем коллекции, которые трогают тесты."""
    for col in (db.seasons_col, db.elo_processing_col, db.player_ratings_col, db.elo_history_col):
        col.delete_many({})
    yield
    db.seasons_col.delete_many({})


# ---------- Репозиторий ----------
async def test_first_season_created_when_none():
    client = mongomock.MongoClient().db
    repo = SeasonsRepo(wrap(client.seasons), wrap(client.elo_processing))

    assert await repo.active_season(NOW) == 1
    assert client.seasons.count_documents({"is_active": True}) == 1


async def test_start_season_closes_old_and_skips_pending_matches():
    client = mongomock.MongoClient().db
    repo = SeasonsRepo(wrap(client.seasons), wrap(client.elo_processing))
    client.seasons.insert_one({"season": 2, "is_active": True})
    client.elo_processing.insert_many([{"match_uuid": "a", "processed": False}, {"match_uuid": "b", "processed": True}])

    await repo.start_season(3, NOW)

    assert await repo.active_season(NOW) == 3
    assert client.seasons.find_one({"season": 2})["ended_at"] == NOW
    assert client.elo_processing.find_one({"match_uuid": "a"})["reason"] == "season_reset"
    assert "skipped" not in client.elo_processing.find_one({"match_uuid": "b"})


# ---------- Главное: новый сезон сразу везде ----------
async def test_reset_applies_everywhere_without_restart(clean_db):
    from lobby_bot.features.elo import service as elo_service
    from lobby_bot.features.elo.repo import EloRepo
    from lobby_bot.features.leaderboard import service as leaderboard_service
    from lobby_bot.features.leaderboard.repo import LeaderboardRepo

    seasons = db.seasons()
    leaderboard_repo = LeaderboardRepo(
        *(
            wrap(c)
            for c in (
                db.player_ratings_col,
                db.history_player_stats_col,
                db.elo_history_col,
                db.discord_panel_col,
                db.profile_views_col,
            )
        ),
        seasons,
    )

    db.seasons_col.insert_one({"season": 1, "is_active": True})
    db.player_ratings_col.insert_many(
        [
            {"steam_id": "S1", "season": 1, "ranked_games": 3, "elo": 1600},
            {
                "steam_id": "S2",
                "season": 2,
                "ranked_games": 1,
                "elo": 1500,
                "last_match_date": datetime.now(UTC).replace(tzinfo=None) - timedelta(days=30),
            },
        ]
    )
    assert [p["steam_id"] for p in await leaderboard_service.fetch_all_players(leaderboard_repo)] == ["S1"]

    assert await start_new_season(seasons, 2) == 1

    assert await current_season(seasons) == 2
    assert [p["steam_id"] for p in await leaderboard_service.fetch_all_players(leaderboard_repo)] == ["S2"]  # лидерборд
    elo_repo = EloRepo(
        *(
            wrap(c)
            for c in (
                db.player_ratings_col,
                db.history_player_stats_col,
                db.elo_history_col,
                db.matches_col,
                db.elo_processing_col,
                db.elo_overrides_col,
            )
        )
    )
    db.player_ratings_col.update_one({"steam_id": "S2"}, {"$set": {"elo": 1700}})
    decayed = await elo_service.process_elo_decay(elo_repo, await current_season(seasons))  # decay
    assert [p["name"] for p in decayed] == ["SteamID: S2"]
    assert db.elo_history_col.find_one({"steam_id": "S2"})["season"] == 2


# ---------- /reset_season ----------
class Response:
    def __init__(self):
        self.messages = []

    async def send_message(self, text, ephemeral):
        self.messages.append(text)

    async def defer(self, ephemeral):
        pass


def interaction(role_ids=()):
    response, followup = Response(), Response()
    followup.send = followup.send_message
    user = SimpleNamespace(id=10, roles=[SimpleNamespace(id=r) for r in role_ids])
    return SimpleNamespace(user=user, response=response, followup=followup)


async def test_reset_season_command(clean_db):
    db.seasons_col.insert_one({"season": 4, "is_active": True})
    cog = SeasonsCog(bot=None, repo=db.seasons())

    denied = interaction()
    await cog.reset_season.callback(cog, denied, 5)
    allowed = interaction(role_ids=(settings.moderator_role_id,))
    await cog.reset_season.callback(cog, allowed, 5)

    assert denied.response.messages == ["❌ Недостаточно прав."]
    assert allowed.followup.messages == ["✅ Сезон **4** завершён. Начат сезон **5**."]
    assert await current_season(db.seasons()) == 5


# ---------- Защита от возврата бага ----------
def _project_files():
    skip = {"tests", ".venv", "venv", "telegram_polls"}
    return [p for p in ROOT.rglob("*.py") if not skip & set(p.relative_to(ROOT).parts)]


def test_no_season_constant_or_frozen_default():
    """Сезон нельзя хранить в константе или вычислять в значении аргумента по умолчанию."""
    problems = []
    for path in _project_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "CURRENT_SEASON":
                problems.append(f"{path.name}:{node.lineno} CURRENT_SEASON")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for default in node.args.defaults + [d for d in node.args.kw_defaults if d]:
                    if any(
                        isinstance(n, ast.Call) and getattr(n.func, "id", "") == "current_season"
                        for n in ast.walk(default)
                    ):
                        problems.append(f"{path.name}:{node.lineno} current_season() по умолчанию")
    assert problems == []
