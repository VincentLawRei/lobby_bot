"""Лидерборд: исправленные баги, сообщения в каналах, команды и кнопки."""

from types import SimpleNamespace

from lobby_bot.features.leaderboard import cog, service
from lobby_bot.features.leaderboard.repo import LeaderboardRepo
from lobby_bot.settings import settings
from tests import mongo as db
from tests.async_mongo import wrap

from .conftest import SEASON, FakeChannel, FakeMessage, interaction

BOT_USER = SimpleNamespace(id=1)


def repo() -> LeaderboardRepo:
    return LeaderboardRepo(
        wrap(db.player_ratings_col),
        wrap(db.history_player_stats_col),
        wrap(db.elo_history_col),
        wrap(db.discord_panel_col),
        wrap(db.profile_views_col),
        db.seasons(),
    )


def make_cog(channels=None):
    channels = channels or {}
    bot = SimpleNamespace(user=BOT_USER, get_channel=lambda cid: channels.get(cid))
    return cog.LeaderboardCog(
        bot, repo(), leaderboard_channel_id=11, duos_channel_id=22, moderator_role_id=settings.moderator_role_id
    )


# ---------- Исправленные баги ----------
async def test_team_wr_shown_without_pairs(data):
    """Раньше топы WR за предателей и мирных пропадали, если не было антидуэтов (ошибка отступа)."""
    blocks = await service.traitors_stats(repo(), SEASON)
    blocks['top_pairs'] = blocks['anti_pairs'] = []
    text = cog.build_duos_embed(SEASON, blocks).description
    assert 'Недостаточно данных' in text
    assert 'Топ-10 WR за Traitors' in text and 'Топ-10 WR за мирных' in text


async def test_season_player_card_uses_that_season(data):
    """Раньше /season_player брал матчи, график и историю из текущего сезона."""
    inter = interaction()
    c = make_cog()
    player = (await service.fetch_all_players(repo(), 2))[0]

    await c.season_player.callback(c, inter, service.get_display_name(player), 2)

    [sent] = inter.response.sent
    embed, view = sent['embed'], sent['view']
    assert embed.footer.text == 'Сезон 2 (архив)'
    recent = next(f.value for f in embed.fields if f.name == '🕓 Последние матчи')
    season_2_matches = db.elo_history_col.count_documents({'steam_id': player['steam_id'], 'season': 2})
    assert season_2_matches and recent  # матчи есть и взяты из сезона 2:
    assert (
        '({})'.format(
            db.elo_history_col.find_one({'steam_id': player['steam_id'], 'season': 2}, sort=[('match_datetime', -1)])[
                'elo_after'
            ]
        )
        in recent
    )
    assert view.season == 2  # кнопка «История матчей» тоже по сезону 2


async def test_elo_event_updates_leaderboard(data):
    channel, duos = FakeChannel(), FakeChannel()
    c = make_cog({11: channel, 22: duos})

    await c.on_elo_matches_processed([('m', SEASON)])

    assert len(channel.sent) == 1 and len(duos.sent) == 1


# ---------- Сообщения в каналах ----------
async def test_update_edits_saved_message(data):
    msg, duos_msg = FakeMessage(5), FakeMessage(6)
    db.discord_panel_col.insert_many(
        [{'key': 'leaderboard_message_id', 'value': 5}, {'key': 'duos_message_id', 'value': 6}]
    )
    channel, duos = FakeChannel([msg]), FakeChannel([duos_msg])

    await make_cog({11: channel, 22: duos}).update_leaderboard()

    assert channel.sent == [] and duos.sent == []
    [edit] = msg.edits
    assert edit['embed'].title == f'🏆 ELO Таблица — Сезон {SEASON}'
    assert isinstance(edit['view'], cog.FullTableButton)
    assert duos_msg.edits[0]['embed'].title.startswith('👹 Traitors Stats')
    assert 'view' not in duos_msg.edits[0]


async def test_update_recreates_deleted_message(data):
    db.discord_panel_col.insert_one({'key': 'leaderboard_message_id', 'value': 404})
    channel = FakeChannel()

    await make_cog({11: channel}).update_leaderboard()

    assert len(channel.sent) == 1
    assert db.discord_panel_col.find_one({'key': 'leaderboard_message_id'})['value'] == 1000


async def test_update_finds_old_message_in_history(data):
    mine = FakeMessage(77, author=BOT_USER, title='🏆 ELO Таблица — Сезон 2')
    other = FakeMessage(78, author=SimpleNamespace(id=2), title='🏆 ELO Таблица — чужое')
    channel = FakeChannel(history=[other, mine])

    await make_cog({11: channel}).update_leaderboard()

    assert channel.sent == [] and len(mine.edits) == 1 and other.edits == []
    assert db.discord_panel_col.find_one({'key': 'leaderboard_message_id'})['value'] == 77


async def test_update_task_survives_errors(data, monkeypatch):
    c = make_cog({11: FakeChannel()})

    async def broken():
        raise ConnectionError("discord down")

    monkeypatch.setattr(c, "update_leaderboard", broken)
    await c.leaderboard_update_task.coro(c)  # не бросает: иначе задача остановилась бы


# ---------- Команды ----------
async def test_player_by_autocomplete_index_and_by_name(data):
    c = make_cog()
    players = await service.fetch_all_players(repo())

    by_index = interaction()
    await c.player.callback(c, by_index, '2')
    assert by_index.response.sent[0]['embed'].title.endswith(service.get_display_name(players[2]))

    by_name = interaction()
    await c.player.callback(c, by_name, 'Ник из модерки')
    assert 'Ник из модерки' in by_name.response.sent[0]['embed'].title

    missing = interaction()
    await c.player.callback(c, missing, 'zzzzzzzzzzzz')
    assert missing.response.sent[0]['content'] == '❌ Игрок **zzzzzzzzzzzz** не найден.'


async def test_change_nickname_requires_moderator_role(data):
    c = make_cog()
    denied = interaction(role_ids=())
    await c.change_rating_nickname.callback(c, denied, '0', 'Новый')
    assert denied.response.sent[0]['content'] == '❌ Недостаточно прав. Требуется роль модератора.'

    mod = interaction(role_ids=(settings.moderator_role_id,))
    player = (await service.fetch_all_players(repo()))[0]
    await c.change_rating_nickname.callback(c, mod, '0', '  Новый  ')
    assert db.player_ratings_col.count_documents(
        {'steam_id': player['steam_id'], 'changed_nickname': 'Новый'}
    ) == db.player_ratings_col.count_documents({'steam_id': player['steam_id']})  # во всех сезонах

    reset = interaction(role_ids=(settings.moderator_role_id,))
    await c.change_rating_nickname.callback(c, reset, '0', '')
    assert (
        db.player_ratings_col.count_documents({'steam_id': player['steam_id'], 'changed_nickname': {'$exists': True}})
        == 0
    )
    assert 'сброшен' in reset.response.sent[0]['content']


async def test_season_commands_without_data(data):
    c = make_cog()
    for command, args in ((c.season_leaderboard, (9,)), (c.season_stats, (9,)), (c.season_player, ('x', 9))):
        inter = interaction()
        await command.callback(c, inter, *args)
        assert inter.response.sent[0]['content'] == '❌ Нет данных для сезона 9.'


async def test_season_leaderboard_titles(data):
    c = make_cog()
    current, past = interaction(), interaction()
    await c.season_leaderboard.callback(c, current, SEASON)
    await c.season_leaderboard.callback(c, past, 2)
    assert current.response.sent[0]['embed'].title == f'🏆 ELO Таблица — Сезон {SEASON} (текущий)'
    assert past.response.sent[0]['embed'].title == '🏆 ELO Таблица — Сезон 2 (архив)'


# ---------- Кнопки ----------
async def test_full_table_buttons_are_persistent(data):
    view = cog.FullTableButton(repo())
    assert view.is_persistent()
    assert {item.custom_id for item in view.children} == {'lb_open_full', 'lb_past_seasons'}

    inter = interaction()
    await view.past_seasons.callback(inter)
    assert [o.value for o in inter.response.sent[0]['view'].children[0].options] == ['2', '1']


async def test_table_pages_and_player_select(data):
    players = await service.fetch_all_players(repo())
    view = cog.LeaderboardView(repo(), players, SEASON)
    inter = interaction()

    await view.next_page(inter)

    assert view.page == 1
    assert inter.response.edited[0]['embed'].footer.text.startswith('Стр. 2/')
    select = next(i for i in view.children if isinstance(i, cog.PlayerSelectMenu))
    assert select.options[0].label.startswith('#26 ')

    pick = interaction()
    select._values = ['0']  # discord.py хранит выбор здесь
    await select.callback(pick)
    assert pick.response.sent[0]['embed'].title.startswith('🥇 ')


async def test_card_history_button(data):
    player = (await service.fetch_all_players(repo()))[0]
    view = cog.PlayerCardView(repo(), player['steam_id'], 'Имя', SEASON)
    inter = interaction()

    await view.show_history.callback(inter)

    sent = inter.response.sent[0]
    assert sent['embed'].title == '🕓 История матчей — Имя'
    assert isinstance(sent['view'], cog.MatchHistoryView)
