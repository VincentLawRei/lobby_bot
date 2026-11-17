"""Клиент Twitch Helix API: токен приложения и список стримов. Про Discord ничего не знает."""

import logging

import aiohttp

logger = logging.getLogger(__name__)

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
STREAMS_URL = "https://api.twitch.tv/helix/streams"


class TwitchApi:
    def __init__(self, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def get_streams(self, **params) -> list[dict]:
        """Активные стримы по фильтрам Helix (game_id, user_login, first...).
        Если токен протух (401), получает новый и повторяет запрос один раз."""
        for attempt in range(2):
            if not self._token:
                await self._refresh_token()
            headers = {"Client-ID": self._client_id, "Authorization": f"Bearer {self._token}"}
            async with self._session.get(STREAMS_URL, headers=headers, params=params) as resp:
                if resp.status == 401 and attempt == 0:
                    self._token = None
                    continue
                resp.raise_for_status()
                data = await resp.json()
                return data.get("data", [])
        return []

    async def _refresh_token(self) -> None:
        params = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "client_credentials",
        }
        async with self._session.post(TOKEN_URL, params=params) as resp:
            resp.raise_for_status()
            self._token = (await resp.json())["access_token"]
        logger.info("✅ Twitch токен обновлён")
