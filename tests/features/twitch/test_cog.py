from lobby_bot.settings import settings

from .conftest import make_stream


async def run(task, cog):
    """Одна итерация @tasks.loop без запуска цикла."""
    await task.coro(cog)


# ---------- Стрим владельца ----------
async def test_owner_offline_sends_nothing(cog, twitch, channels):
    await run(cog.my_stream_task, cog)

    assert channels["my_stream"].sent == []


async def test_owner_stream_notified_once_with_role_ping(cog, twitch, channels):
    twitch.live = [make_stream(settings.my_twitch_login, "S1")]

    await run(cog.my_stream_task, cog)
    await run(cog.my_stream_task, cog)

    sent = channels["my_stream"].sent
    assert len(sent) == 1
    assert sent[0][0] == f"<@&{settings.my_stream_role_id}>"


async def test_new_owner_stream_notified_again(cog, twitch, channels):
    twitch.live = [make_stream(settings.my_twitch_login, "S1")]
    await run(cog.my_stream_task, cog)
    twitch.live = [make_stream(settings.my_twitch_login, "S2")]

    await run(cog.my_stream_task, cog)

    assert len(channels["my_stream"].sent) == 2


async def test_failed_send_is_retried_next_time(cog, twitch, channels, repo):
    twitch.live = [make_stream(settings.my_twitch_login, "S1")]
    channels["my_stream"].fail_send = True

    await run(cog.my_stream_task, cog)
    assert await repo.get_last_notified_stream("channel_1") is None  # не запомнили неотправленное

    channels["my_stream"].fail_send = False
    await run(cog.my_stream_task, cog)
    assert len(channels["my_stream"].sent) == 1


async def test_api_error_does_not_break_task(cog, twitch, channels):
    twitch.fail_status = 500

    await run(cog.my_stream_task, cog)  # не бросает исключение: иначе задача остановилась бы

    assert channels["my_stream"].sent == []


# ---------- Стримеры категории ----------
async def test_monitor_notifies_only_new_russian_streamers(cog, twitch, channels):
    twitch.live = [make_stream("ru1", "1"), make_stream("en1", "2", language="en")]
    await run(cog.monitor_task, cog)

    twitch.live = [make_stream("ru1", "1"), make_stream("ru2", "3")]
    await run(cog.monitor_task, cog)

    titles = [embed.title for _, embed in channels["notify"].sent]
    assert titles == ["ru1 начал стримить!", "ru2 начал стримить!"]


async def test_monitor_list_message_created_then_edited(cog, twitch, channels):
    twitch.live = [make_stream("ru1", "1")]
    await run(cog.monitor_task, cog)
    twitch.live = []
    await run(cog.monitor_task, cog)

    assert len(channels["list"].sent) == 1
    assert channels["list"].edits[-1].title == "Сейчас никто не стримит"


async def test_monitor_list_recreated_when_deleted(cog, twitch, channels):
    await run(cog.monitor_task, cog)
    channels["list"].messages.clear()  # сообщение удалили в Discord

    await run(cog.monitor_task, cog)

    assert len(channels["list"].sent) == 2
