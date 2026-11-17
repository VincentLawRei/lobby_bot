from types import SimpleNamespace

from lobby_bot.features.match_stats.cog import MatchStatsCog


class FakeImporter:
    def __init__(self, result):
        self.result = result
        self.repo = SimpleNamespace(ensure_indexes=self._no_indexes)

    async def _no_indexes(self):
        return None

    async def run(self):
        return self.result


class EventBot:
    def __init__(self):
        self.events = []

    def dispatch(self, event, *args):
        self.events.append((event, *args))


async def test_casual_matches_sent_as_one_event():
    bot = EventBot()
    cog = MatchStatsCog(bot=bot, importer=FakeImporter(["m1", "m2"]))

    await cog.import_task.coro(cog)

    assert bot.events == [("casual_matches_imported", ["m1", "m2"])]  # итоги отправит match_results


async def test_no_event_without_casual_matches():
    bot = EventBot()
    cog = MatchStatsCog(bot=bot, importer=FakeImporter([]))

    await cog.import_task.coro(cog)

    assert bot.events == []


async def test_import_error_does_not_break_task():
    class Broken(FakeImporter):
        async def run(self):
            raise OSError("disk")

    cog = MatchStatsCog(bot=None, importer=Broken([]))

    await cog.import_task.coro(cog)  # не бросает исключение: иначе задача остановилась бы
