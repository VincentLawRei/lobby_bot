"""Database смотрит в коллекции MongoDB с прежними именами.

Опечатка в имени коллекции не падает: бот просто начнёт читать и писать пустую коллекцию.
"""

from lobby_bot.db import Database

# Атрибуты, у которых имя коллекции в MongoDB другое; у остальных совпадает с атрибутом
RENAMED = {"roles": "roles_and_emojis", "state": "bot_state", "player_reports": "player_reports_col"}
COLLECTIONS = {
    "roles",
    "state",
    "dm_logs",
    "players",
    "lobbies",
    "twitch",
    "discord_hosts_presets",
    "discord_hosts_notify",
    "discord_panel",
    "player_reports",
    "console_access",
    "player_locations",
    "profile_views",
    "match_damage",
    "game_matches",
    "history_player_stats",
    "player_ratings",
    "elo_processing",
    "elo_history",
    "matches",
    "seasons",
    "elo_overrides",
}


def test_collection_names_unchanged():
    database = Database.connect("mongodb://localhost:27017")
    attributes = {name for name in vars(database) if name != "client"}
    assert attributes == COLLECTIONS
    for name in attributes:
        assert getattr(database, name).name == RENAMED.get(name, name), name


async def test_close():
    await Database.connect("mongodb://localhost:27017").close()
