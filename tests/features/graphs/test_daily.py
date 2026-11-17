import json
from datetime import datetime

from lobby_bot.features.graphs.daily import (
    build_daily_graph_html,
    latest_names_by_game_id,
)


def test_latest_names_prefer_newest_moderator_nick():
    players = [
        {"game_uid": "D1", "nickname": "a", "date": datetime(2026, 1, 1)},
        {"game_uid": "D1", "nickname": "b", "date": datetime(2026, 2, 1)},
        {"game_uid": "D2", "nickname": "c", "changed_nickname": "Старый", "date": datetime(2026, 1, 1)},
        {"game_uid": "D2", "nickname": "d", "changed_nickname": "Новый", "date": "2026-03-01T00:00:00Z"},
    ]

    assert latest_names_by_game_id(players) == {"D1": "b", "D2": "Новый"}


def test_daily_html_describes_lobbies_in_tooltips():
    lobbies = [{"players": ["D1", "D2"], "host": "D1"}, {"players": ["D2", "D3"], "host": "D3"}]
    players = [
        {"game_uid": d, "nickname": n, "date": datetime(2026, 1, 1)}
        for d, n in (("D1", "Crab"), ("D2", "Bob"), ("D3", "Zed"))
    ]

    html = build_daily_graph_html(lobbies, players)

    def in_html(text):  # pyvis пишет данные узлов в JSON с экранированием не-ASCII символов
        return json.dumps(text)[1:-1] in html

    assert in_html("Crab — хостил 1 раз(а):\nЛобби 1: Crab, Bob")
    assert in_html("Bob\nИграл в 2 лобби:")
    assert in_html("Crab и Bob ИГРАЛИ друг с другом 1 раз")
