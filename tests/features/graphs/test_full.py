from datetime import UTC, datetime, timedelta

from lobby_bot.features.graphs.full import (
    ago_long,
    ago_short,
    build_full_graph_html,
    build_lobby_graph,
    community_color,
    elo_table,
    game_to_steam_map,
    latest_nicknames,
    player_stats,
    remove_weak_links,
    top_active,
)
from lobby_bot.features.graphs.repo import FullGraphData

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def test_ago_long():
    assert ago_long(None, NOW) == "неизвестно"
    assert ago_long(NOW - timedelta(minutes=5), NOW) == "5 мин. назад"
    assert ago_long((NOW - timedelta(hours=3)).replace(tzinfo=None), NOW) == "3 ч. назад"  # наивная дата = UTC
    assert ago_long(NOW - timedelta(days=1), NOW) == "вчера"
    assert ago_long(NOW - timedelta(days=15), NOW) == "2 нед. назад"
    assert ago_long(NOW - timedelta(days=65), NOW) == "2 мес. назад"


def test_ago_short():
    assert ago_short(None, NOW) == "?"
    assert ago_short(NOW - timedelta(minutes=10), NOW) == "сейчас"
    assert ago_short(NOW - timedelta(minutes=50), NOW) == "50м"
    assert ago_short(NOW - timedelta(hours=5), NOW) == "5ч"
    assert ago_short(NOW - timedelta(days=4), NOW) == "4д"


def test_nicknames_from_newest_record_and_moderator_nick_wins():
    players = [  # новые первыми
        {"other_id": "S1", "game_uid": "D1", "nickname": "new", "changed_nickname": None},
        {"other_id": "S1", "game_uid": "D1", "nickname": "old", "changed_nickname": "Мод"},
        {"other_id": "S2", "game_uid": "D2", "nickname": "bob", "changed_nickname": "Бобёр"},
    ]

    assert latest_nicknames(players) == {"S1": "new", "S2": "Бобёр"}
    assert game_to_steam_map(players) == {"D1": "S1", "D2": "S2"}


def test_graph_supports_old_and_new_snapshot_formats():
    lobbies = [
        {"players": ["D1", "D2", "D3"], "host": "D1", "timestamp": datetime(2026, 9, 1)},
        {
            "players": [{"other_id": "S1"}, {"other_id": "S2"}],
            "host": {"other_id": "S2"},
            "timestamp": datetime(2026, 9, 2),
        },
    ]
    graph, hosts, last_game = build_lobby_graph(lobbies, {"D1": "S1", "D2": "S2", "D3": "S3"})

    assert graph["S1"]["S2"]["weight"] == 2 and graph["S1"]["S3"]["weight"] == 1
    assert hosts == {"S1": 1, "S2": 1}
    assert last_game["S1"] == datetime(2026, 9, 2)


def test_weak_links_and_lonely_players_removed():
    lobbies = [{"players": ["A", "B", "C"]}] * 5 + [{"players": ["A", "D"]}]
    graph, _, _ = build_lobby_graph(lobbies, {x: x for x in "ABCD"})

    remove_weak_links(graph)

    assert set(graph.nodes) == {"A", "B", "C"}


def test_player_stats():
    rows = [
        {
            "_id": "S1",
            "total_games": 4,
            "total_wins": 3,
            "traitors_games": 1,
            "traitors_wins": 1,
            "crew_games": 3,
            "crew_wins": 2,
            "roles": ["Quartermaster", "Quartermaster", "Medic"],
        }
    ]

    assert player_stats(rows)["S1"] == {
        "overall_wr": 75.0,
        "traitors_wr": 100.0,
        "crew_wr": 66.7,
        "traitors_pick": 25,
        "crew_pick": 75,
        "most_common_role": "Quartermaster",
        "total_games": 4,
    }


def test_top_active_counts_only_period():
    lobbies = [
        {"players": ["D1", "D2"], "timestamp": (NOW - timedelta(hours=1)).replace(tzinfo=None)},
        {"players": ["D1"], "timestamp": (NOW - timedelta(days=3)).replace(tzinfo=None)},
    ]

    top = top_active(lobbies, {"D1": "S1", "D2": "S2"}, {"S1": "Crab"}, {}, NOW - timedelta(days=7), NOW)

    assert [(t["nickname"], t["games"]) for t in top] == [("Crab", 2), ("S2", 1)]


def test_elo_table():
    rows = [{"current_name": "Crab", "changed_nickname": "Краб", "elo": 1234.6, "ranked_games": 4, "ranked_wins": 1}]

    assert elo_table(rows) == [{"rank": 1, "name": "Краб", "elo": 1235, "games": 4, "wins": 1, "losses": 3, "wr": 25.0}]


def test_community_color_stable_for_small_communities():
    big = [(i, ["x"]) for i in range(20)]
    assert community_color(0, big) == "#FF6B6B"
    assert community_color(19, big) == community_color(19, big)


def test_full_html_has_graph_and_filled_overlay():
    lobbies = [
        {"players": ["D1", "D2", "D3"], "host": "D1", "timestamp": (NOW - timedelta(hours=2)).replace(tzinfo=None)}
    ] * 6
    players = [{"other_id": f"S{i}", "game_uid": f"D{i}", "nickname": f"P{i}"} for i in (1, 2, 3)]
    data = FullGraphData(lobbies=lobbies, players=players, stats_rows=[], elo_rows=[])

    html = build_full_graph_html(data, NOW)

    assert '"label": "P1"' in html and "</body>" in html
    assert "__" + "COMMUNITIES_JSON__" not in html  # все подстановки заполнены
    assert "const topDailyData = [{\"nickname\": \"P1\", \"games\": 6" in html
