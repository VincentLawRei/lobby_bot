"""/report: шаги, embed в лог и сохранённый репорт сверяются с эталонами в snapshots/."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from lobby_bot.features.reports import cog, service
from lobby_bot.features.reports.repo import ReportsRepo
from lobby_bot.settings import settings
from tests import mongo as db
from tests.async_mongo import wrap
from tests.snapshot import assert_snapshot

NOW = datetime(2026, 10, 20, 12, 0)


def repo() -> ReportsRepo:
    return ReportsRepo(wrap(db.player_reports_col), wrap(db.player_ratings_col))


@pytest.fixture
def data(monkeypatch):
    monkeypatch.setattr(service, "now_utc", lambda: NOW)
    for col in (db.seasons_col, db.player_ratings_col, db.player_reports_col):
        col.delete_many({})
    db.seasons_col.insert_one({'season': 4, 'is_active': True})
    db.player_ratings_col.insert_many(
        [
            {'steam_id': 'S1', 'season': 4, 'current_name': 'Alpha'},
            {'steam_id': 'S2', 'season': 4, 'current_name': 'Bravo', 'changed_nickname': 'Брат'},
            {'steam_id': 'S3', 'season': 4, 'current_name': 'Charlie', 'changed_nickname': 'Charlie'},
            {'steam_id': 'S4', 'season': 4},
            {'steam_id': 'S1', 'season': 3, 'current_name': 'Старый'},
        ]
    )
    yield
    for col in (db.seasons_col, db.player_ratings_col, db.player_reports_col):
        col.delete_many({})


class Log:
    def __init__(self):
        self.embeds = []

    async def send(self, embed):
        self.embeds.append(embed.to_dict())


class Recorder:
    def __init__(self):
        self.log = []

    async def send_message(self, content=None, **kw):
        view = kw.get('view')
        items = (
            [
                (type(i).__name__, i.placeholder, i.min_values, i.max_values, [o.label for o in i.options])
                for i in view.children
            ]
            if view
            else None
        )
        self.log.append(('send', content, items, view.timeout if view else None, kw.get('ephemeral')))
        self.view = view

    async def edit_message(self, **kw):
        self.log.append(('edit', kw.get('content'), kw.get('view')))


def interaction(log=None):
    user = SimpleNamespace(id=42, display_name='Репортер', mention='<@42>')
    return SimpleNamespace(
        user=user,
        response=Recorder(),
        client=SimpleNamespace(get_channel=lambda cid: log if cid == settings.reports_log_channel_id else None),
    )


@pytest.mark.parametrize(
    "player,anonymous", [('S1', False), ('S2', True), ('S3', False), ('S4', False), ('nope', False)]
)
async def test_report_flow(data, request, player, anonymous):
    log = Log()
    start = interaction(log)
    c = cog.ReportsCog(None, repo(), db.seasons(), settings.reports_log_channel_id)
    await c.report.callback(c, start, player=player, anonymous=anonymous)
    steps = [start.response.log]
    view = getattr(start.response, 'view', None)
    if view:
        select = view.children[0]
        select._values = ['Читы', 'По рофлу']
        done = interaction(log)
        await select.callback(done)
        steps.append(done.response.log)
    assert_snapshot(
        request, {'steps': steps, 'log': log.embeds, 'reports': list(db.player_reports_col.find({}, {'_id': 0}))}
    )


async def test_report_without_log_channel(data):
    c = cog.ReportsCog(None, repo(), db.seasons(), settings.reports_log_channel_id)
    start = interaction()
    await c.report.callback(c, start, player='S1', anonymous=False)
    select = start.response.view.children[0]
    select._values = ['Консоль']
    done = interaction(None)
    await select.callback(done)
    assert done.response.log == [('edit', '✅ Репорт отправлен. Спасибо!', None)]
    assert db.player_reports_col.count_documents({}) == 1


@pytest.mark.parametrize("current", ['', 'al', 'брат', 'zzz', 'CHAR'])
async def test_autocomplete(data, request, current):
    c = cog.ReportsCog(None, repo(), db.seasons(), 1)
    choices = await c.report_player_autocomplete(interaction(), current)
    assert_snapshot(request, [(x.name, x.value) for x in choices])


def test_slash_command_unchanged(request):
    import discord
    from discord.ext import commands

    bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
    [command] = cog.ReportsCog(bot, repo(), db.seasons(), 1).get_app_commands()
    assert_snapshot(request, command.to_dict(bot.tree))


def test_reasons_unchanged():
    assert service.REPORT_REASONS == [
        "Читы",
        "Консоль",
        "Негативное поведение",
        "Отсутствие тумана",
        "По рофлу",
        "Троллинг в игре",
    ]
