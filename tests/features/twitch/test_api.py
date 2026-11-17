import aiohttp
import pytest

from .conftest import make_stream


async def test_token_requested_once_and_reused(api, twitch):
    twitch.live = [make_stream("streamer", "1")]

    assert len(await api.get_streams()) == 1
    assert len(await api.get_streams()) == 1
    assert twitch.token_requests == 1


async def test_expired_token_is_refreshed_and_request_retried(api, twitch):
    twitch.live = [make_stream("streamer", "1")]
    await api.get_streams()
    twitch.expire_token_once = True

    streams = await api.get_streams()

    assert len(streams) == 1  # повтор после 401 вернул данные в том же вызове
    assert twitch.token_requests == 2
    assert twitch.last_auth == "Bearer token2"


async def test_filters_passed_to_twitch(api, twitch):
    twitch.live = [make_stream("owner", "1"), make_stream("other", "2")]

    streams = await api.get_streams(user_login="owner")

    assert [s["user_login"] for s in streams] == ["owner"]


async def test_server_error_raises_client_error(api, twitch):
    twitch.fail_status = 500

    with pytest.raises(aiohttp.ClientResponseError):
        await api.get_streams()
