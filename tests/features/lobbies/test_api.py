"""EOS API лобби: разбор сессии, получение и обновление токена, пропуск неразборчивых лобби (HTTP подменён)."""

from lobby_bot.features.lobbies.api import (
    LOBBIES_FILTER,
    LOBBIES_URL,
    TOKEN_URL,
    EosLobbyApi,
    parse_session,
)

SESSION = {
    "totalPlayers": 3,
    "openPublicPlayers": 5,
    "owner": "owner-id",
    "publicPlayers": ["D1", "D2"],
    "attributes": {
        "LOBBYNAME_s": "Crab",
        "ONLINESTARTED_b": False,
        "MISSION_s": "Outpost",
        "SERVERREGION_s": "eu",
        "NATIVESESSIONID_s": "native",
        "AGE_l": 7,
        "LANGUAGE_s": "RU",
        "LISTENSERVERREADY_b": True,
        "DESIREDLEADERID_s": "_+_S1|D1",
        "MEMBERS_s": "D1:S1:Crab;D2:S2:Bob Smith;",
    },
}


def test_parse_session():
    lobby = parse_session(SESSION)

    assert lobby["lobby_host_name"] == "Crab"
    assert lobby["leader_id"] == ["S1", "D1"]
    assert lobby["lobby_players"] == [
        {"id": "D1", "diff_id": "S1", "name": "Crab"},
        {"id": "D2", "diff_id": "S2", "name": "Bob Smith"},
    ]
    assert (lobby["total_players"], lobby["open_public_players"], lobby["is_server_listening"]) == (3, 5, True)
    assert lobby["uri"] == ""


class FakeResponse:
    def __init__(self, data):
        self.data = data

    async def json(self, content_type=None):
        return self.data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """aiohttp.ClientSession: ответы по URL по очереди, запросы записываются."""

    def __init__(self, responses):
        self.responses = {url: list(items) for url, items in responses.items()}
        self.requests = []

    def post(self, url, headers, **kwargs):
        self.requests.append((url, headers["Authorization"], kwargs))
        return FakeResponse(self.responses[url].pop(0))


def api_with(responses) -> tuple[EosLobbyApi, FakeSession]:
    api = EosLobbyApi(
        "Basic client", {"external_auth_token": "t", "deployment_id": "d", "nonce": "n", "display_name": "bot"}
    )
    api._session = FakeSession(responses)
    return api, api._session


async def test_first_call_gets_token_then_lobbies():
    api, session = api_with(
        {TOKEN_URL: [{"access_token": "T1"}], LOBBIES_URL: [{"sessions": [SESSION, {"attributes": {}}]}]}
    )

    lobbies = await api.get_lobbies()

    assert [lobby["lobby_host_name"] for lobby in lobbies] == ["Crab"]  # неразборчивое лобби пропущено
    (token_url, token_auth, token_kwargs), (lobbies_url, lobbies_auth, lobbies_kwargs) = session.requests
    assert (token_url, token_auth) == (TOKEN_URL, "Basic client")
    assert token_kwargs["data"]["grant_type"] == "external_auth" and token_kwargs["data"]["nonce"] == "n"
    assert (lobbies_url, lobbies_auth, lobbies_kwargs["json"]) == (LOBBIES_URL, "Bearer T1", LOBBIES_FILTER)


async def test_expired_token_is_refreshed_and_call_returns_empty():
    api, session = api_with(
        {TOKEN_URL: [{"access_token": "T2"}], LOBBIES_URL: [{"errorCode": "expired"}, {"sessions": [SESSION]}]}
    )
    api._token = "old"

    assert await api.get_lobbies() == []
    assert api._token == "T2"
    assert len(await api.get_lobbies()) == 1
    assert [r[1] for r in session.requests] == ["Bearer old", "Basic client", "Bearer T2"]
