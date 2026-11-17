"""Асинхронный интерфейс pymongo (AsyncMongoClient) поверх mongomock для тестов.

mongomock умеет только синхронный API. Обёртка повторяет форму асинхронного: методы коллекции
awaitable, `find()` возвращает курсор без await (sort/limit/skip, `to_list`, `async for`),
`aggregate()` awaitable и возвращает курсор. Данные хранит обёрнутый mongomock, поэтому тест
может готовить их синхронно (`AsyncCollection.sync`) и проверять тем же способом.
"""

from __future__ import annotations

from typing import Any


class AsyncCursor:
    def __init__(self, cursor) -> None:
        self._cursor = cursor

    def sort(self, *args, **kwargs) -> AsyncCursor:
        self._cursor = self._cursor.sort(*args, **kwargs)
        return self

    def limit(self, n: int) -> AsyncCursor:
        self._cursor = self._cursor.limit(n)
        return self

    def skip(self, n: int) -> AsyncCursor:
        self._cursor = self._cursor.skip(n)
        return self

    async def to_list(self, length: int | None = None) -> list[dict]:
        docs = list(self._cursor)
        return docs if length is None else docs[:length]

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for doc in self._cursor:
            yield doc


class AsyncCollection:
    def __init__(self, collection) -> None:
        self.sync = collection
        self.name = collection.name

    def find(self, *args, **kwargs) -> AsyncCursor:
        return AsyncCursor(self.sync.find(*args, **kwargs))

    async def aggregate(self, pipeline, *args, **kwargs) -> AsyncCursor:
        return AsyncCursor(self.sync.aggregate(pipeline, *args, **kwargs))

    def __getattr__(self, name: str) -> Any:
        method = getattr(self.sync, name)

        async def call(*args, **kwargs):
            return method(*args, **kwargs)

        return call


class AsyncDatabase:
    def __init__(self, database) -> None:
        self.sync = database

    def __getitem__(self, name: str) -> AsyncCollection:
        return AsyncCollection(self.sync[name])


class AsyncMongoClient:
    """Подменяет pymongo.AsyncMongoClient. Без аргумента client работает на общем mongomock-клиенте
    тестов: его же видит синхронный доступ tests/mongo.py."""

    shared = None  # задаёт tests/conftest.py

    def __init__(self, *args, client=None, **kwargs) -> None:
        self.sync = client if client is not None else self.shared

    def __getitem__(self, name: str) -> AsyncDatabase:
        return AsyncDatabase(self.sync[name])

    async def close(self) -> None:
        pass


def wrap(collection) -> AsyncCollection:
    """Синхронная mongomock-коллекция теста в асинхронную для репозитория."""
    return AsyncCollection(collection)
