"""Точка входа: `python -m lobby_bot`. Сам бот и порядок старта описаны в app.py, фичи в lobby_bot/features/."""

from lobby_bot.logging_setup import setup_logging

# Настраиваем логи до импорта остальных модулей, чтобы сообщения при импорте тоже попали в файл
setup_logging()

from lobby_bot.app import bot  # noqa: E402
from lobby_bot.settings import settings  # noqa: E402


def run():
    # log_handler=None: discord.py не ставит свой обработчик, его логи идут в наш (logging_setup)
    bot.run(settings.discord_token.get_secret_value(), log_handler=None)


if __name__ == "__main__":
    run()
