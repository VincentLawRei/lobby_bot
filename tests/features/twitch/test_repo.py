from lobby_bot.features.twitch.repo import TwitchStateRepo


async def test_empty_state(repo: TwitchStateRepo):
    assert await repo.get_stream_list_message_id() is None
    assert await repo.get_last_notified_stream("channel_1") is None


async def test_state_is_saved(repo: TwitchStateRepo):
    await repo.set_stream_list_message_id(42)
    await repo.set_last_notified_stream("channel_1", "stream-1")
    await repo.set_last_notified_stream("channel_1", "stream-2")

    assert await repo.get_stream_list_message_id() == 42
    assert await repo.get_last_notified_stream("channel_1") == "stream-2"
    assert await repo.get_last_notified_stream("channel_2") is None


async def test_document_ids_compatible_with_old_twitch_py(repo: TwitchStateRepo):
    """В проде уже лежат документы со старыми _id: если их сменить, бот повторит уведомления."""
    await repo.set_stream_list_message_id(1)
    await repo.set_last_notified_stream("channel_1", "s")

    ids = {doc["_id"] async for doc in repo._col.find()}

    assert ids == {"stream_list_msg", "my_stream_last:channel_1"}
