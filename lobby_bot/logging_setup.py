"""Настройка логирования: консоль + файл logs/bot.log с ротацией."""

import logging
import os
from logging.handlers import RotatingFileHandler

# logs/ в корне проекта (на уровень выше пакета): в Docker это /app/logs, примонтированный как ./logs
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if root.handlers:  # уже настроено (повторный вызов)
        return

    os.makedirs(LOG_DIR, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT)

    console = logging.StreamHandler()
    file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "bot.log"), maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    for handler in (console, file_handler):
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(level)

    # paramiko пишет в INFO каждое SSH-подключение, это шум
    logging.getLogger("paramiko").setLevel(logging.WARNING)
