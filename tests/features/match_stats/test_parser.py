from datetime import datetime
from pathlib import Path

from lobby_bot.features.match_stats.parser import (
    damage_doc,
    is_valid_match,
    normalize_map,
    parse_csv_file,
    parse_duration_to_seconds,
    player_stat_docs,
)

FIXTURES = Path(__file__).parent / "fixtures"
FULL = FIXTURES / "AAAA0000000000000000000000000001.csv"  # UTF-16, 8 игроков, 28 минут
SHORT = FIXTURES / "BBBB0000000000000000000000000002.csv"  # ASCII, 2 игрока, 3 минуты
NOW = datetime(2026, 2, 10)


def test_parse_utf16_csv():
    match, players = parse_csv_file(str(FULL))

    assert match["UUID"] == "AAAA0000000000000000000000000001" and match["Map"] == "Station"
    assert len(players) == 8
    assert players[0] == {
        "platform": "Steam",
        "steam_id": "76561000000000000",
        "game_uid": "eos00",
        "team": "Traitors",
        "role": "COMMANDER",
        "name": "Игрок0",
        "rating": "B+",
        "deaths": 0,
    }


def test_parse_ascii_csv():
    match, players = parse_csv_file(str(SHORT))

    assert match["Outcome"] == "Crew Won" and len(players) == 2


def test_incomplete_csv(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("UUID,FinishTime\nX,1\n")

    assert parse_csv_file(str(path)) == (None, None)


def test_duration():
    assert parse_duration_to_seconds("+00:23:03.000") == 1383
    assert parse_duration_to_seconds("garbage") == 0
    assert parse_duration_to_seconds(None) == 0


def test_valid_match_needs_5_minutes_and_8_players():
    assert is_valid_match({"Duration": "+00:05:00.000"}, 8)
    assert not is_valid_match({"Duration": "+00:04:59.000"}, 8)
    assert not is_valid_match({"Duration": "+00:30:00.000"}, 7)


def test_player_docs():
    match, players = parse_csv_file(str(FULL))

    docs = player_stat_docs(match, players, is_ranked=True, season=3, now=NOW)

    assert [d["won"] for d in docs] == [True, True] + [False] * 6  # "Traitors Won"
    first = docs[0]
    assert first["match_datetime"] == datetime(2026, 2, 8, 19, 46, 50)
    assert (first["match_duration_seconds"], first["escape_progress"], first["players_count"]) == (1701, 86, 8)
    assert (first["is_ranked"], first["is_valid_match"], first["season"], first["imported_at"]) == (True, True, 3, NOW)


def test_damage_totals_by_side_and_weapon():
    data = {
        "match_id": "M",
        "started_at": "2026-02-08T19:00:00Z",
        "ended_at": "bad",
        "winner": "Traitors",
        "players": {"T": {"name": "t", "isTraitor": True}, "S": {"name": "s"}},
        "damage": {"T": {"S": {"axe": 10, "gun": 5}}, "S": {"T": {"gun": 7}, "S": {"gun": 1}}},
    }

    doc = damage_doc(data, NOW)

    assert doc["players"]["T"]["damage_to_crew"] == 15
    assert doc["players"]["S"]["damage_to_traitors"] == 7
    assert doc["players"]["S"]["damage_to_crew"] == 1  # урон по своим тоже считается
    assert doc["players"]["S"]["damage_by_weapons"] == {"gun": 8}
    assert doc["started_at"] is None and doc["ended_at"] is None  # одна битая дата обнуляет обе, как раньше


def test_normalize_map():
    assert normalize_map(" Station ") == "harbor"
    assert normalize_map("Outpost") == "outpost"
    assert normalize_map(None) is None
