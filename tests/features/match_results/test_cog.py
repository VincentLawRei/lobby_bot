"""Итоги матчей: отправленные embed'ы, кнопки и меню под ними, тексты статистики и репорт.

Вывод сверяется с эталонами в snapshots/.
"""

import pytest

from lobby_bot.features.match_results import cog, service
from lobby_bot.features.match_results.repo import MatchResultsRepo
from lobby_bot.features.reports.repo import ReportsRepo
from tests import mongo as db
from tests.async_mongo import wrap
from tests.snapshot import assert_snapshot

from .conftest import (
    CASUAL,
    CREW,
    RANKED,
    SEASON,
    THR,
    Channel,
    add_main_account_change,
    interaction,
    view_shape,
)


def fake_bot(results, penalty):
    from types import SimpleNamespace

    from lobby_bot.settings import settings

    channels = {settings.match_results_channel_id: results, settings.penalty_log_channel_id: penalty}
    return SimpleNamespace(get_channel=lambda cid: channels.get(cid))


def repo() -> MatchResultsRepo:
    return MatchResultsRepo(
        wrap(db.history_player_stats_col),
        wrap(db.player_ratings_col),
        wrap(db.elo_history_col),
        wrap(db.match_damage_col),
    )


def reports_repo() -> ReportsRepo:
    return ReportsRepo(wrap(db.player_reports_col), wrap(db.player_ratings_col))


def new_cog(results, penalty):
    from lobby_bot.settings import settings

    return cog.MatchResultsCog(
        fake_bot(results, penalty),
        repo(),
        reports_repo(),
        settings.match_results_channel_id,
        settings.penalty_log_channel_id,
        settings.site_url,
    )


def sent_shape(channel: Channel) -> list:
    return [(m['embed'].to_dict(), view_shape(m['view']) if m.get('view') else None) for m in channel.sent]


async def test_ranked_notification(data, request):
    results, penalty = Channel(), Channel()
    await new_cog(results, penalty).on_elo_matches_processed([(RANKED, SEASON)])

    assert_snapshot(request, {'results': sent_shape(results), 'penalty': sent_shape(penalty)})
    assert len(penalty.sent) == 2  # штрафы и твинк
    assert 'без изменений' in penalty.sent[1]['embed'].description


async def test_main_account_change_does_not_break_other_matches(data):
    """Ошибка одного матча уходит в лог и не мешает отправке остальных."""
    add_main_account_change()
    results = Channel()
    await new_cog(results, Channel()).on_elo_matches_processed([(RANKED, SEASON), (RANKED, SEASON)])
    assert results.sent == []  # оба раза тот же сломанный матч: см. тест ниже


@pytest.mark.xfail(
    raises=KeyError,
    strict=True,
    reason="известная проблема (CLAUDE.md): у записи начисления основному за твинка нет поля team",
)
async def test_main_account_change_still_sends_results(data):
    """Матч, где ELO твинка ушло основному, тоже должен попасть в канал итогов. Когда баг починят, xfail
    перестанет срабатывать (strict) и пометку надо снять."""
    add_main_account_change()
    results = Channel()
    await new_cog(results, Channel()).send_ranked(RANKED, SEASON)
    assert len(results.sent) == 1


async def test_ranked_without_penalty_channel(data, request):
    results = Channel()
    await new_cog(results, None).send_ranked(RANKED, SEASON)
    assert_snapshot(request, sent_shape(results))


async def test_ranked_without_damage_and_penalties(data, request):
    db.match_damage_col.delete_many({})
    db.player_ratings_col.update_many({}, {'$set': {'is_penalized': False}})
    results, penalty = Channel(), Channel()
    await new_cog(results, penalty).send_ranked(RANKED, SEASON)
    assert_snapshot(request, {'results': sent_shape(results), 'penalty': sent_shape(penalty)})


async def test_casual_notification(data, request):
    results = Channel()
    await new_cog(results, None).on_casual_matches_imported([CASUAL])
    assert_snapshot(request, sent_shape(results))


async def test_unknown_match_sends_nothing(data):
    results = Channel()
    c = new_cog(results, Channel())
    await c.send_ranked('nope', SEASON)
    await c.send_casual('nope')
    assert results.sent == []


@pytest.mark.parametrize("steam_id", CREW + THR + ['EXTRA', 'none', 'missing'])
async def test_player_detail(data, request, steam_id):
    inter = interaction()
    menu = cog.PlayerSelectMenu(RANKED, repo())
    menu.item._values = [steam_id]
    await menu.callback(inter)
    assert_snapshot(request, inter.response.sent)


async def test_player_detail_without_damage(data):
    db.match_damage_col.delete_many({})
    inter = interaction()
    menu = cog.PlayerSelectMenu(RANKED, repo())
    menu.item._values = ['S1']
    await menu.callback(inter)
    assert inter.response.sent == [{'content': '❌ Данные урона не найдены.', 'ephemeral': True}]


async def test_report_flow(data, request):
    log = Channel()
    start = interaction(log)
    await cog.ReportMatchButton(RANKED, repo(), reports_repo()).callback(start)
    sent = start.response.sent[0]
    view = sent['view']
    initial_shape = view_shape(view)  # до нажатий: подписи меняются по ходу

    steps = []
    submit = next(i for i in view.children if type(i).__name__ == 'ReportSubmitButton')
    empty = interaction(log)
    await submit.callback(empty)  # без игрока
    steps.append(empty.response.sent)

    player_select = view.children[0]
    player_select._values = ['T1']
    step = interaction(log)
    await player_select.callback(step)
    steps.append(step.response.edited[0]['content'])

    reasons = view.children[1]
    reasons._values = ['Читы', 'Троллинг в игре']
    step = interaction(log)
    await reasons.callback(step)
    steps.append(step.response.edited[0]['content'])

    toggle = view.children[2]
    step = interaction(log)
    await toggle.callback(step)
    steps.append((toggle.label, step.response.edited[0]['content']))

    done = interaction(log)
    await submit.callback(done)
    steps.append(done.response.edited)

    report = db.player_reports_col.find_one({}, {'_id': 0})
    assert_snapshot(
        request,
        {
            'content': sent['content'],
            'initial_view': initial_shape,
            'final_view': view_shape(view),
            'steps': steps,
            'report': report,
            'log': [e['embed'].to_dict() for e in log.sent],
        },
    )
    assert report['target_steam_id'] == 'T1' and report['anonymous'] is True


async def test_report_without_players(data):
    db.history_player_stats_col.delete_many({})
    db.match_damage_col.delete_many({})
    inter = interaction()
    await cog.ReportMatchButton(RANKED, repo(), reports_repo()).callback(inter)
    assert inter.response.sent == [{'content': '❌ Игроки матча не найдены.', 'ephemeral': True}]


def test_slash_commands_none():
    """У фичи нет своих команд (только кнопки и меню)."""
    assert cog.MatchResultsCog(None, repo(), reports_repo(), 1, 2, 'x').get_app_commands() == []


def test_damage_texts(data, request):
    doc = db.match_damage_col.find_one({'match_id': RANKED})
    names = {sid: f'n{sid}' for sid in CREW + THR}
    crew = [{'steam_id': s} for s in CREW]
    traitors = [{'steam_id': s} for s in THR]
    assert_snapshot(
        request,
        {
            'stats': service.format_damage_stats(doc['players'], doc['raw_damage'], crew, traitors, names),
            'details': {
                sid: service.build_player_detail_text(sid, doc['players'], doc['raw_damage'], names)
                for sid in CREW + THR + ['EXTRA']
            },
        },
    )


def test_wildlife_damage_uses_weapon_names():
    """Урон «от природы» не только от животных (заряд): имя из словаря оружия, а не техническое DT_..."""
    players = {'S1': {'role': 'Commander', 'isTraitor': False}}
    raw = {'wildlife': {'S1': {'DT_Explosive_Charge_C': 200, 'DT_BearClaws_C': 50, 'DT_Unknown_C': 5}}}
    text = service.build_player_detail_text('S1', players, raw, {'S1': 'Cap'})
    assert '🌿 Природа — Заряд: 200 · 🐻 Медведь: 50 · DT_Unknown_C: 5' in text
    assert 'DT_Explosive_Charge_C' not in text


async def test_casual_one_team_and_long_names(data, request):
    """Пустая команда («—») и очень длинные имена: обрезка статистики урона по лимиту embed и текста по 2000."""
    db.history_player_stats_col.delete_many({'match_uuid': CASUAL, 'team': 'Crew'})
    db.history_player_stats_col.update_many({}, [{'$set': {'name': {'$concat': ['$name', '_' + 'x' * 900]}}}])
    results = Channel()
    c = new_cog(results, None)
    await c.send_casual(CASUAL)
    await c.send_ranked(RANKED, SEASON)
    assert results.sent[0]['embed'].fields[0].value == '—'
    ranked = results.sent[1]['embed']
    # Лимит считается до подписи снизу, поэтому с огромными именами embed может выйти за 6000
    assert ranked.fields[-1].name == '📊 Статистика урона' and ranked.fields[-1].value.endswith('...')  # обрезан

    details = {}
    for sid in ('S1', 'T1'):
        inter = interaction()
        menu = cog.PlayerSelectMenu(RANKED, repo())
        menu.item._values = [sid]
        await menu.callback(inter)
        details[sid] = inter.response.sent
    assert_snapshot(request, {'sent': sent_shape(results), 'details': details})


async def test_ranked_one_team(data, request):
    db.elo_history_col.delete_many({'team': 'Crew'})
    results = Channel()
    await new_cog(results, None).send_ranked(RANKED, SEASON)
    assert results.sent[0]['embed'].fields[0].value == '—'
    assert_snapshot(request, sent_shape(results))


async def test_persistent_items_take_repos_from_cog():
    """После рестарта меню и кнопку создаёт Discord (from_custom_id): репозитории берутся у Cog."""
    import re
    from types import SimpleNamespace

    c = new_cog(Channel(), None)
    inter = SimpleNamespace(client=SimpleNamespace(get_cog=lambda name: c if name == 'MatchResultsCog' else None))
    menu = await cog.PlayerSelectMenu.from_custom_id(
        inter, None, re.match(r'stats:(?P<match_id>.+)', f'stats:{RANKED}')
    )
    button = await cog.ReportMatchButton.from_custom_id(
        inter, None, re.match(r'report:(?P<match_id>.+)', f'report:{RANKED}')
    )
    assert (menu.match_id, menu.repo) == (RANKED, c.repo)
    assert (button.match_id, button.repo, button.reports_repo) == (RANKED, c.repo, c.reports_repo)
