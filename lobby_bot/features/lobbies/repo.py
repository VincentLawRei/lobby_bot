"""Данные мониторинга лобби в MongoDB.

- `players`: каждый увиденный игрок (ник, Social Deduction Game ID, Steam ID, счётчик появлений);
- `lobbies`: снапшоты заполненных русских лобби (по ним строятся графы игроков);
- `roles_and_emojis`: хосты = роли со steam_id (заводятся в фиче roles);
- `bot_state`, ключи `notify_<роль>`: сообщение-уведомление о лобби хоста и время его создания.
"""

from datetime import datetime, timedelta

from pymongo.asynchronous.collection import AsyncCollection

SNAPSHOT_INTERVAL = timedelta(minutes=30)  # не чаще одного снапшота лобби за это время
NOTIFY_PREFIX = "notify_"

# По game_uid: последний игровой ник и все ники, заданные модератором (от новых к старым)
PUBLIC_PLAYERS_PIPELINE = [
    {"$sort": {"date": -1}},
    {
        "$group": {
            "_id": "$game_uid",
            "latest_nickname": {"$first": "$nickname"},
            "changed_nicknames": {"$push": "$changed_nickname"},
        }
    },
]


class LobbiesRepo:
    def __init__(
        self, players: AsyncCollection, lobbies: AsyncCollection, roles: AsyncCollection, state: AsyncCollection
    ) -> None:
        self._players = players
        self._lobbies = lobbies
        self._roles = roles
        self._state = state

    # ---------- Игроки ----------
    async def upsert_player(self, nickname: str, game_uid: str, steam_id: str, now: datetime) -> None:
        if await self._players.find_one({"other_id": steam_id}):
            await self._players.update_one(
                {"other_id": steam_id},
                {
                    "$set": {"nickname": nickname, "game_uid": game_uid, "date": now},
                    "$inc": {"counter": 1},
                },
            )
        else:
            await self._players.insert_one(
                {
                    "nickname": nickname,
                    "game_uid": game_uid,
                    "other_id": steam_id,
                    "date": now,
                    "counter": 1,
                    "changed_nickname": None,
                }
            )

    async def changed_nickname(self, steam_id: str) -> str | None:
        """Ник, заданный модератором через !nick."""
        doc = await self._players.find_one({"changed_nickname": {"$ne": None}, "other_id": steam_id})
        return doc["changed_nickname"] if doc else None

    async def public_player_names(self, game_uids: list[str]) -> tuple[list[str], int]:
        """(известные ники игроков, число неизвестных игроков)."""
        cursor = await self._players.aggregate([{"$match": {"game_uid": {"$in": game_uids}}}, *PUBLIC_PLAYERS_PIPELINE])
        docs = await cursor.to_list()
        # Ник от модератора (самый свежий непустой) важнее игрового
        names = [next((c for c in doc["changed_nicknames"] if c), None) or doc["latest_nickname"] for doc in docs]
        missing = len(set(game_uids) - {doc["_id"] for doc in docs})
        return names, missing

    # ---------- Снапшоты ----------
    async def save_snapshot(self, lobby: dict, now: datetime) -> None:
        """Снапшот русского лобби для графа игроков, не чаще раза в SNAPSHOT_INTERVAL."""
        if lobby.get("language") != "RU":
            return
        session_id = lobby.get("lobby_native_session_id")
        last = await self._lobbies.find_one({"lobby_native_session_id": session_id}, sort=[("timestamp", -1)])
        if last and now - last["timestamp"] < SNAPSHOT_INTERVAL:
            return
        await self._lobbies.insert_one(
            {
                "timestamp": now,
                "host": lobby.get("owner"),
                "lobby_native_session_id": session_id,
                "players": lobby.get("public_players_ids"),
            }
        )

    # ---------- Хосты и уведомления ----------
    async def hosts_by_steam_id(self) -> dict[str, str]:
        """steam_id -> имя роли хоста."""
        return {doc["steam_id"]: doc["role"] async for doc in self._roles.find({"steam_id": {"$exists": True}})}

    async def host_role(self, role: str) -> dict | None:
        return await self._roles.find_one({"role": role})

    async def get_notify_state(self, role: str) -> tuple[int | None, str | None]:
        """(ID сообщения-уведомления, время создания строкой '%Y-%m-%d %H:%M:%S%z')."""
        doc = await self._state.find_one({"_id": NOTIFY_PREFIX + role})
        return (doc["msg_id"], doc["dt"]) if doc else (None, None)

    async def set_notify_state(self, role: str, message_id: int | None, created: str | None) -> None:
        await self._state.update_one(
            {"_id": NOTIFY_PREFIX + role}, {"$set": {"msg_id": message_id, "dt": created}}, upsert=True
        )

    async def notified_roles(self) -> list[str]:
        return [
            doc["_id"].removeprefix(NOTIFY_PREFIX) async for doc in self._state.find({"_id": {"$regex": "^notify_"}})
        ]
