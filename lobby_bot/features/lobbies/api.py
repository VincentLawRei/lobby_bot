"""Лобби Social Deduction Game из Epic Online Services (EOS) API. Про Discord и БД ничего не знает.

Авторизация как у игрового клиента: ключи GAME_HEADER_AUTHORIZATION, EXTERNAL_AUTH_TOKEN, DEPLOYMENT_ID,
NONCE, DISPLAY_NAME в .env.
"""

import logging
import re

import aiohttp

logger = logging.getLogger(__name__)

TOKEN_URL = "https://api.epicgames.dev/auth/v1/oauth/token"
LOBBIES_URL = "https://api.epicgames.dev/lobby/v1/00000000000000000000000000000000/lobbies/filter"
EOS_HEADERS = {
    "Host": "api.epicgames.dev",
    "Accept-Encoding": "deflate, gzip",
    "Accept": "application/json",
    "User-Agent": "EOS-SDK/1.14.1-18153445 (Windows/10.0.26100.1591.64bit) Game/1.0.0",
    "X-EOS-Version": "1.14.1-18153445",
}
LANGUAGES = [
    "EN",
    "DE",
    "ES",
    "FR",
    "IT",
    "JA",
    "KO",
    "PL",
    "PT-BR",
    "TR",
    "RU",
    "BG",
    "CS",
    "DA",
    "EL",
    "FI",
    "HU",
    "NB",
    "NL",
    "PT",
    "RO",
    "SV",
    "TH",
    "UK",
]
# Открытые и уже запущенные лобби текущей версии игры с выбранной картой
LOBBIES_FILTER = {
    "criteria": [
        {"key": "attributes.LOBBYSEARCH_b", "op": "EQUAL", "value": True},
        {"key": "attributes.LOBBYNAME_s", "op": "NOT_EQUAL", "value": ""},
        {"key": "attributes.VERSION_s", "op": "EQUAL", "value": "v1.2.4"},
        {"key": "attributes.MISSION_s", "op": "NOT_EQUAL", "value": "unset"},
        {"key": "attributes.LANGUAGE_s", "op": "ANY_OF", "value": LANGUAGES},
    ],
    "maxResults": 200,
}
MEMBER_PATTERN = re.compile(r"(\w+:\w+:[^;]+);")  # "game_uid:steam_id:ник;"


class EosLobbyApi:
    def __init__(self, header_authorization: str, auth_form: dict[str, str]) -> None:
        self._header_authorization = header_authorization
        self._auth_form = auth_form  # external_auth_token, deployment_id, nonce, display_name
        self._token: str | None = None
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def get_lobbies(self) -> list[dict]:
        """Текущие лобби. Если токен протух, EOS не присылает sessions: тогда токен
        обновляется, а этот вызов возвращает пустой список (как и раньше)."""
        if not self._token:
            await self._refresh_token()
        headers = {**EOS_HEADERS, "Content-Type": "application/json", "Authorization": f"Bearer {self._token}"}
        async with self._session.post(LOBBIES_URL, headers=headers, json=LOBBIES_FILTER) as resp:
            data = await resp.json(content_type=None)
        if "sessions" not in data:
            await self._refresh_token()
            return []
        lobbies = []
        for session in data["sessions"]:
            try:
                lobbies.append(parse_session(session))
            except (KeyError, TypeError, AttributeError) as e:
                logger.warning("Не удалось разобрать лобби: %r", e)
        return lobbies

    async def _refresh_token(self) -> None:
        headers = {
            **EOS_HEADERS,
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": self._header_authorization,
        }
        form = {"grant_type": "external_auth", "external_auth_type": "deviceid_access_token", **self._auth_form}
        async with self._session.post(TOKEN_URL, headers=headers, data=form) as resp:
            self._token = (await resp.json(content_type=None))["access_token"]
        logger.info("Новый токен EOS получен")


def parse_session(session: dict) -> dict:
    """Сессия EOS -> лобби в формате, который используют мониторинг и снапшоты."""
    attrs = session["attributes"]
    return {
        "lobby_host_name": attrs["LOBBYNAME_s"],
        "is_lobby_started": attrs["ONLINESTARTED_b"],
        "lobby_map": attrs["MISSION_s"],
        "lobby_host_region": attrs["SERVERREGION_s"],
        "lobby_native_session_id": attrs["NATIVESESSIONID_s"],
        "total_players": session["totalPlayers"],
        "open_public_players": session["openPublicPlayers"],
        "time_from_creation": attrs["AGE_l"],
        "is_started": attrs["ONLINESTARTED_b"],
        "owner": session["owner"],
        "public_players_ids": session["publicPlayers"],
        "language": attrs["LANGUAGE_s"],
        "is_server_listening": attrs["LISTENSERVERREADY_b"],
        "uri": attrs.get("URI_s", ""),
        # [steam_id, game_uid] хоста
        "leader_id": attrs["DESIREDLEADERID_s"].replace("_+_", "").split("|"),
        "lobby_players": [
            dict(zip(("id", "diff_id", "name"), member.split(":"), strict=False))
            for member in MEMBER_PATTERN.findall(attrs["MEMBERS_s"])
        ],
    }
