"""Embed'ы лидерборда сверяются с эталонами в snapshots/: таблица, страницы, дуэты, карточка игрока,
история матчей, автокомплит, команды.
"""

import pytest

from lobby_bot.features.leaderboard import cog, service
from lobby_bot.features.leaderboard.repo import LeaderboardRepo
from tests import mongo as db
from tests.async_mongo import wrap
from tests.snapshot import assert_snapshot

from .conftest import SEASON, interaction


def repo() -> LeaderboardRepo:
    return LeaderboardRepo(
        wrap(db.player_ratings_col),
        wrap(db.history_player_stats_col),
        wrap(db.elo_history_col),
        wrap(db.discord_panel_col),
        wrap(db.profile_views_col),
        db.seasons(),
    )


async def test_static_leaderboard(data, request):
    assert_snapshot(
        request, (await cog.build_static_leaderboard_embed(repo(), await service.fetch_all_players(repo()))).to_dict()
    )


@pytest.mark.parametrize("season,page", [(SEASON, 0), (SEASON, 1), (2, 0), (1, 0)])
async def test_leaderboard_pages(data, request, season, page):
    players = await service.fetch_all_players(repo(), season)
    embed = await cog.build_leaderboard_page_embed(repo(), players, page, season)
    assert_snapshot(request, embed.to_dict())


@pytest.mark.parametrize("season", [SEASON, 2, 1])
async def test_traitors_stats(data, request, season):
    embed = await cog.get_duos_embed(repo(), season)
    assert '**Топ дуэты' in embed.description and 'Антидуэты' in embed.description
    assert_snapshot(request, embed.to_dict())


@pytest.mark.parametrize("rank", [1, 2, 3, 5, 15])
async def test_player_card(data, request, rank):
    player = (await service.fetch_all_players(repo()))[rank - 1]
    embed, file = await cog.build_player_card(repo(), player, rank, SEASON)
    assert_snapshot(request, {'embed': embed.to_dict(), 'chart': file is not None})
    if file:
        assert file.fp.read(8) == b'\x89PNG\r\n\x1a\n'


async def test_player_card_past_season(data, request):
    player = (await service.fetch_all_players(repo(), 2))[0]
    embed, _ = await cog.build_player_card(repo(), player, 1, 2)
    assert_snapshot(request, embed.to_dict())


async def test_player_card_counts_views(data):
    player = (await service.fetch_all_players(repo()))[0]
    await cog.build_player_card(repo(), player, 1, SEASON)
    embed, _ = await cog.build_player_card(repo(), player, 1, SEASON)
    assert db.profile_views_col.find_one({'steam_id': player['steam_id']})['views'] == 2
    assert embed.to_dict() is not None


@pytest.mark.parametrize("page", [0, 1, 3])
async def test_match_history(data, request, page):
    player = (await service.fetch_all_players(repo()))[0]
    sid, name = player['steam_id'], service.get_display_name(player)
    assert_snapshot(request, (await cog.build_match_history_embed(repo(), sid, name, page, SEASON)).to_dict())


async def test_history_buttons_page_count(data):
    """Кнопки страниц считают и начисления за твинка (так было раньше)."""
    player = (await service.fetch_all_players(repo()))[0]
    view = await cog.MatchHistoryView.create(repo(), player['steam_id'], 'x', SEASON)
    count = db.elo_history_col.count_documents(
        {'steam_id': player['steam_id'], 'season': SEASON, 'manual': {'$exists': False}}
    )
    assert view.total_pages == service.total_pages(count, cog.MATCHES_PAGE_SIZE)


async def test_nicknames_and_lines(data, request):
    players = await service.fetch_all_players(repo())
    assert any(service.get_display_name(p) == 'Ник из модерки' for p in players)
    assert await service.past_seasons(repo()) == [2, 1]
    assert_snapshot(request, [service.build_player_line(i + 1, p, service.now_utc()) for i, p in enumerate(players)])


async def test_data_covers_rules(data):
    """Данные задевают ветки, иначе эталоны ничего не доказывают."""
    players = await service.fetch_all_players(repo())
    assert len(players) > service.PAGE_SIZE > service.STATIC_TOP
    assert any(not p.get('is_calibrated') for p in players) and any(p.get('is_penalized') for p in players)
    blocks = await service.traitors_stats(repo(), SEASON)
    for key in (
        'top_pairs',
        'anti_pairs',
        'traitors_wr',
        'crew_wr',
        'traitors_win_streaks',
        'crew_loss_streaks',
        'frequent_traitors',
        'eternal_civilians',
        'double_dead',
        'recent_traitors_3d',
        'recent_traitors_7d',
    ):
        assert blocks[key], key


@pytest.mark.parametrize("current", ['', 'Игрок 7656000', 'модер'])
async def test_player_autocomplete(data, request, current):
    from types import SimpleNamespace

    from lobby_bot.settings import settings

    c = cog.LeaderboardCog(SimpleNamespace(), repo(), 11, 22, settings.moderator_role_id)
    choices = await c.player_autocomplete(interaction(), current)
    assert_snapshot(request, [(x.name, x.value) for x in choices])


def test_slash_commands_unchanged(request):
    """Команды для Discord: имена, описания, параметры, автокомплит."""
    import discord
    from discord.ext import commands

    bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
    new_cog = cog.LeaderboardCog(bot, repo(), 1, 2, 3)
    assert_snapshot(request, {c.name: c.to_dict(bot.tree) for c in new_cog.get_app_commands()})
