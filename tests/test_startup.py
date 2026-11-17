"""Смоук-тест старта бота без Discord: всё регистрируется, набор команд не изменился.

Защищает рефакторинг: при переносе фичи в Cog команды не должны пропасть или переименоваться.
Если команда меняется намеренно, обнови tests/commands_snapshot.txt
(список печатается в сообщении упавшего теста).
"""

from pathlib import Path

import pytest
from discord.ext import tasks

SNAPSHOT = Path(__file__).parent / "commands_snapshot.txt"


@pytest.fixture
async def started_bot(monkeypatch):
    from lobby_bot import logging_setup

    monkeypatch.setattr(logging_setup, "setup_logging", lambda: None)  # не писать logs/ из тестов

    from lobby_bot import __main__  # noqa: F401  (точка входа импортируется без ошибок)
    from lobby_bot.app import bot
    from lobby_bot.features.servers.cog import ServersCog

    started: list[str] = []
    monkeypatch.setattr(tasks.Loop, "start", lambda self, *a, **k: started.append(self.coro.__name__))

    async def no_sync():
        return []

    async def no_wait():
        return None

    async def no_panel(self):
        return None

    monkeypatch.setattr(bot.tree, "sync", no_sync)
    monkeypatch.setattr(bot, "wait_until_ready", no_wait)
    monkeypatch.setattr(ServersCog, "create_initial_panel", no_panel)

    await bot.setup_hook()
    await bot.get_cog("ServersCog")._startup_task  # панель и задачи серверов после on_ready
    yield bot, started
    for name in list(bot.extensions):
        await bot.unload_extension(name)


async def test_commands_match_snapshot(started_bot):
    bot, _ = started_bot
    names = sorted([c.name for c in bot.tree.get_commands()] + [f"!{c.name}" for c in bot.commands])
    expected = SNAPSHOT.read_text(encoding="utf-8").split()
    assert names == expected, "Набор команд изменился:\n" + "\n".join(names)


async def test_background_tasks_started(started_bot):
    _, started = started_bot
    assert sorted(started) == sorted(
        [
            "my_stream_task",  # TwitchCog
            "panel_update_task",
            "auto_stop_task",
            "reservation_cleanup_task",
            "failed_start_check_task",  # ServersCog
            "import_task",
            "elo_processing_task",
            "leaderboard_update_task",
            "daily_elo_decay_task",
            "penalty_expiry_task",
        ]
    )


async def test_cogs_and_persistent_items_registered(started_bot):
    bot, _ = started_bot
    assert set(bot.cogs) == {
        "TwitchCog",
        "RolesCog",
        "PlayersCog",
        "DmLogCog",
        "TournamentCog",
        "LobbiesCog",
        "GraphsCog",
        "MatchStatsCog",
        "SeasonsCog",
        "EloCog",
        "LeaderboardCog",
        "MatchResultsCog",
        "EloAdminCog",
        "ConsoleCog",
        "ReportsCog",
        "ServersCog",
    }
    assert {type(v).__name__ for v in bot.persistent_views} == {"ServerPanelView", "FullTableButton"}
    dynamic = {cls.__name__ for cls in bot._connection._view_store._dynamic_items.values()}
    assert dynamic == {"PlayerSelectMenu", "ReportMatchButton", "ShareLocationButton"}
