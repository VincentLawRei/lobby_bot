"""Задачи EloCog: ошибки не останавливают задачи, событие о посчитанных матчах, пост о decay."""

from datetime import datetime
from types import SimpleNamespace

from lobby_bot.features.elo import service
from lobby_bot.features.elo.cog import EloCog

from . import replay
from .conftest import make_match


class FakeChannel:
    def __init__(self):
        self.embeds = []

    async def send(self, embed):
        self.embeds.append(embed)


class EventBot:
    """Бот, который записывает отправленные события."""

    def __init__(self):
        self.events = []

    def dispatch(self, event, *args):
        self.events.append((event, *args))


def fake_repo(queue):
    async def nothing():
        return None

    async def pending():
        return queue

    return SimpleNamespace(ensure_indexes=nothing, pending_matches=pending)


async def _season_7(now):
    return 7


SEASONS = SimpleNamespace(active_season=_season_7)  # сезоны: активный сезон 7


async def test_processing_task_handles_each_match(monkeypatch):
    processed = []

    async def process(repo, match_uuid, season):
        if match_uuid == 'crash':
            raise ValueError("bad data")
        processed.append(match_uuid)
        return match_uuid != 'skip'

    monkeypatch.setattr(service, "process_queued_match", process)
    queue = [{'match_uuid': u} for u in ('m1', 'crash', 'skip', 'm2')]
    repo = fake_repo(queue)
    bot = EventBot()
    cog = EloCog(bot=bot, repo=repo, seasons=SEASONS, log_channel_id=1)

    await cog.elo_processing_task.coro(cog)

    assert processed == ['m1', 'skip', 'm2']  # ошибка одного матча не мешает остальным
    # Одно событие со всеми посчитанными по порядку: итоги в канал (match_results), таблица (leaderboard)
    assert bot.events == [('elo_matches_processed', [('m1', 7), ('m2', 7)])]


async def test_no_event_without_new_matches():
    bot = EventBot()
    cog = EloCog(bot=bot, repo=fake_repo([]), seasons=SEASONS, log_channel_id=1)

    await cog.elo_processing_task.coro(cog)

    assert bot.events == []


async def test_processing_task_survives_database_error():
    async def broken():
        raise ConnectionError("mongo down")

    repo = fake_repo([])
    repo.pending_matches = broken
    cog = EloCog(bot=None, repo=repo, seasons=SEASONS, log_channel_id=1)
    await cog.elo_processing_task.coro(cog)  # не бросает: иначе задача остановилась бы навсегда


async def test_decay_posts_to_log_channel(monkeypatch):
    channel = FakeChannel()
    bot = SimpleNamespace(get_channel=lambda channel_id: channel if channel_id == 55 else None)

    async def decay(repo, season):
        return [{'name': 'Bob', 'old_elo': 1700, 'new_elo': 1650}]

    monkeypatch.setattr(service, "process_elo_decay", decay)

    cog = EloCog(bot, repo=None, seasons=SEASONS, log_channel_id=55)

    await cog.daily_elo_decay_task.coro(cog)

    [embed] = channel.embeds
    assert embed.fields[0].value == "• **Bob**: 1700 ➔ `1650` ELO (`-50`)\n"


async def test_decay_without_inactive_players_posts_nothing(monkeypatch):
    channel = FakeChannel()
    bot = SimpleNamespace(get_channel=lambda channel_id: channel)

    async def decay(repo, season):
        return []

    monkeypatch.setattr(service, "process_elo_decay", decay)
    cog = EloCog(bot, repo=None, seasons=SEASONS, log_channel_id=1)

    await cog.daily_elo_decay_task.coro(cog)

    assert channel.embeds == []


async def test_decay_error_does_not_break_task(monkeypatch):
    async def broken(repo, season):
        raise ConnectionError("mongo down")

    monkeypatch.setattr(service, "process_elo_decay", broken)
    cog = EloCog(bot=None, repo=None, seasons=SEASONS, log_channel_id=1)
    await cog.daily_elo_decay_task.coro(cog)


async def test_processing_task_end_to_end():
    """Настоящий расчёт на базе в памяти: матч из очереди посчитан и отмечен."""
    database = replay.new_database()
    database.history_player_stats.insert_many(
        make_match('real', [('A', 'Crew'), ('B', 'Traitors')], traitors_won=False, when=datetime(2026, 10, 1))
    )
    database.elo_processing.insert_one({'match_uuid': 'real', 'processed': False})
    bot = EventBot()
    cog = EloCog(bot=bot, repo=replay.make_repo(database), seasons=SEASONS, log_channel_id=1)

    await cog.elo_processing_task.coro(cog)

    assert bot.events == [('elo_matches_processed', [('real', 7)])]
    assert database.player_ratings.find_one({'steam_id': 'A'})['season'] == 7
