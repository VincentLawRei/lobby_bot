"""MongoDB: асинхронный клиент (Database).

Database создаёт бот при старте (LobbyBot.setup_hook) и закрывает при остановке (LobbyBot.close).
Фичи получают коллекции из `bot.db` в своём setup() и передают их в репозитории через конструктор.
"""

from pymongo import AsyncMongoClient
from pymongo.asynchronous.collection import AsyncCollection

DB_NAME = "sdgame"


class Database:
    """Асинхронное подключение и коллекции. Имена коллекций в MongoDB не меняются."""

    def __init__(self, client: AsyncMongoClient) -> None:
        self.client = client
        db = client[DB_NAME]
        self.roles: AsyncCollection = db["roles_and_emojis"]
        self.state: AsyncCollection = db["bot_state"]
        self.dm_logs: AsyncCollection = db["dm_logs"]
        self.players: AsyncCollection = db["players"]
        self.lobbies: AsyncCollection = db["lobbies"]
        self.twitch: AsyncCollection = db["twitch"]
        self.discord_hosts_presets: AsyncCollection = db["discord_hosts_presets"]
        self.discord_hosts_notify: AsyncCollection = db["discord_hosts_notify"]
        self.discord_panel: AsyncCollection = db["discord_panel"]
        self.player_reports: AsyncCollection = db["player_reports_col"]
        self.console_access: AsyncCollection = db["console_access"]
        self.player_locations: AsyncCollection = db["player_locations"]
        self.profile_views: AsyncCollection = db["profile_views"]
        # Ranked
        self.match_damage: AsyncCollection = db["match_damage"]
        self.game_matches: AsyncCollection = db["game_matches"]  # лобби с модами (создаётся при хосте)
        self.history_player_stats: AsyncCollection = db["history_player_stats"]  # история матчей каждого игрока
        self.player_ratings: AsyncCollection = db["player_ratings"]  # текущий рейтинг игроков
        self.elo_processing: AsyncCollection = db["elo_processing"]  # очередь обработки ELO
        self.elo_history: AsyncCollection = db["elo_history"]  # история изменений ELO
        self.matches: AsyncCollection = db["matches"]  # агрегированные данные матчей
        self.seasons: AsyncCollection = db["seasons"]
        self.elo_overrides: AsyncCollection = db["elo_overrides"]  # ручные бонусы веса на предателя

    @classmethod
    def connect(cls, url: str) -> "Database":
        """Клиент без запросов: подключение к серверу открывается при первой операции."""
        return cls(AsyncMongoClient(url))

    async def close(self) -> None:
        await self.client.close()
