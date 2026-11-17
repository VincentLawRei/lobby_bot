"""Синхронный доступ тестов к общей базе в памяти: подготовить данные и проверить, что записал код.

Коллекции те же, что у `lobby_bot.db.Database`, под именами `<атрибут Database>_col`
(`mongo.player_ratings_col`). Код бота работает с ними асинхронно через `bot.db` или `wrap(col)`.
"""

from lobby_bot.db import Database

_database = Database.connect("mongodb://localhost:27017")  # mongomock из tests/conftest.py

for _name, _collection in vars(_database).items():
    if _name != "client":
        globals()[f"{_name}_col"] = _collection.sync


def seasons():
    """SeasonsRepo на общей базе: текущий сезон для кода, которому он нужен."""
    from lobby_bot.features.seasons.repo import SeasonsRepo
    from tests.async_mongo import wrap

    return SeasonsRepo(wrap(seasons_col), wrap(elo_processing_col))  # noqa: F821
