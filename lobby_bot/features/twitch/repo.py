"""Состояние Twitch-оповещений в MongoDB (коллекция `twitch`).

_id документов те же, что были в twitch.py, поэтому после обновления бот не шлёт повторных уведомлений.
"""

from pymongo.asynchronous.collection import AsyncCollection

STREAM_LIST_DOC_ID = "stream_list_msg"
MY_STREAM_DOC_PREFIX = "my_stream_last:"


class TwitchStateRepo:
    def __init__(self, collection: AsyncCollection) -> None:
        self._col = collection

    async def get_stream_list_message_id(self) -> int | None:
        doc = await self._col.find_one({"_id": STREAM_LIST_DOC_ID})
        return doc.get("msg_id") if doc else None

    async def set_stream_list_message_id(self, message_id: int) -> None:
        await self._col.update_one({"_id": STREAM_LIST_DOC_ID}, {"$set": {"msg_id": message_id}}, upsert=True)

    async def get_last_notified_stream(self, target: str) -> str | None:
        """ID последнего стрима, о котором уже уведомили в target (имя канала-получателя)."""
        doc = await self._col.find_one({"_id": MY_STREAM_DOC_PREFIX + target})
        return doc.get("stream_id") if doc else None

    async def set_last_notified_stream(self, target: str, stream_id: str) -> None:
        await self._col.update_one(
            {"_id": MY_STREAM_DOC_PREFIX + target}, {"$set": {"stream_id": stream_id}}, upsert=True
        )
