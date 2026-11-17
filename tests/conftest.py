"""Общая подготовка тестов.

Тесты не трогают реальные сервисы:
- настройки берутся из фейковых значений для всех ключей .env.example (реальный .env не нужен);
- pymongo.AsyncMongoClient подменён на mongomock (база в памяти, асинхронный интерфейс в tests/async_mongo.py);
  синхронный доступ тестов к той же базе: tests/mongo.py.
Всё это выполняется до импорта кода бота, поэтому стоит в начале файла.
"""

import os
import re
import time
from pathlib import Path

import mongomock
import pymongo

from tests import async_mongo

ROOT = Path(__file__).resolve().parent.parent


def _fake_env() -> dict[str, str]:
    """Фейковое значение для каждого ключа из .env.example: ID числами, остальное строками."""
    env = {}
    keys = re.findall(r"^([A-Z][A-Z0-9_]*)=", (ROOT / ".env.example").read_text(encoding="utf-8"), re.M)
    for n, key in enumerate(keys, start=1):
        if key.endswith("_ENABLED"):
            env[key] = "false"
        elif key.endswith("_IDS"):
            env[key] = f"{n}1,{n}2"
        elif key.endswith("_ID") or "_ID_" in key:
            env[key] = str(1000 + n)
        else:
            env[key] = f"test_{key.lower()}"
    env["DISABLED_SERVERS"] = "0,1,2"  # все серверы отключены: так записаны эталоны servers (сценарии включают сами)
    env["MONGO_URL"] = "mongodb://localhost:27017"
    env["SITE_URL"] = "https://example.org"
    return env


os.environ.update(_fake_env())

# Часовой пояс как в Docker-образе (Dockerfile: TZ=Europe/Moscow): ошибки с naive datetime видны и локально
os.environ["TZ"] = "Europe/Moscow"
if hasattr(time, "tzset"):  # на Windows tzset нет: там действует пояс системы
    time.tzset()
async_mongo.AsyncMongoClient.shared = mongomock.MongoClient()
pymongo.AsyncMongoClient = async_mongo.AsyncMongoClient
