"""Игровые серверы в MongoDB.

- `discord_hosts_notify`: состояние сервера (ключ `server` = server_id): текущий хост, порт запуска,
  ротация портов `current_port`, резерв;
- `discord_hosts_presets`: пресеты запуска пользователя (карта, моды, режим, регион) и
  пользовательские модификаторы `custom_modifiers` / `custom_modifiers_enabled`;
- `discord_panel`: ID сообщения панели серверов (`_id: panel_message`);
- `game_matches`: матчи, запущенные через бота (`guuid`, `is_ranked_mods`, моды);
- `player_ratings`, `history_player_stats`: только чтение, для ELO-кеша и винрейтов пар на серверах.
"""

from datetime import datetime

from pymongo.asynchronous.collection import AsyncCollection


class ServersRepo:
    def __init__(
        self,
        state: AsyncCollection,
        presets: AsyncCollection,
        panel: AsyncCollection,
        matches: AsyncCollection,
        ratings: AsyncCollection,
        stats: AsyncCollection,
    ) -> None:
        self._state = state
        self._presets = presets
        self._panel = panel
        self._matches = matches
        self._ratings = ratings
        self._stats = stats

    # ---------- Состояние сервера ----------
    async def state(self, server_id: str) -> dict | None:
        return await self._state.find_one({"server": server_id})

    async def set_state(self, server_id: str, fields: dict) -> None:
        await self._state.update_one({"server": server_id}, {"$set": fields}, upsert=True)

    async def unset_state(self, server_id: str, fields: list[str]) -> None:
        await self._state.update_one({"server": server_id}, {"$unset": dict.fromkeys(fields, "")})

    # ---------- Пресеты и модификаторы ----------
    async def preset(self, user_id: str) -> dict | None:
        return await self._presets.find_one({"user_id": user_id})

    async def set_preset_fields(self, user_id: str, fields: dict) -> None:
        await self._presets.update_one({"user_id": user_id}, {"$set": fields}, upsert=True)

    # ---------- Панель и матчи ----------
    async def panel_message_id(self) -> int | None:
        doc = await self._panel.find_one({"_id": "panel_message"})
        return doc.get("message_id") if doc else None

    async def save_panel_message_id(self, message_id: int) -> None:
        await self._panel.update_one({"_id": "panel_message"}, {"$set": {"message_id": message_id}}, upsert=True)

    async def save_match(self, guuid: str, is_ranked: bool, mods: list[str], now: datetime) -> None:
        await self._matches.insert_one({"guuid": guuid, "is_ranked_mods": is_ranked, "mods": mods, "created_at": now})

    # ---------- Данные для игровых серверов ----------
    async def season_ratings(self, season: int) -> list[dict]:
        return await self._ratings.find(
            {"season": season},
            {
                "steam_id": 1,
                "elo": 1,
                "wins": 1,
                "losses": 1,
                "total_games": 1,
                "current_name": 1,
                "changed_nickname": 1,
                "traitors_games": 1,
                "traitors_wins": 1,
                "crew_games": 1,
                "crew_wins": 1,
                "role_stats": 1,
                "is_calibrated": 1,
                "calibration_games": 1,
                "crew_winstreak": 1,
                "traitor_winstreak": 1,
                "_id": 0,
            },
        ).to_list()

    async def ranked_traitor_rows(self) -> list[dict]:
        """Строки предателей во всех валидных рейтинговых матчах (все сезоны)."""
        return await self._stats.find(
            {"is_ranked": True, "is_valid_match": True, "team": "Traitors"},
            {"match_uuid": 1, "steam_id": 1, "name": 1, "won": 1, "_id": 0},
        ).to_list()
