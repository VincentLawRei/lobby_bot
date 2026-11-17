"""Настройки бота из переменных окружения и .env.

Все чувствительные значения (токены, адреса серверов, ID каналов и ролей, личные ссылки)
хранятся только в .env. Шаблон со всеми ключами: .env.example.

Имя поля = имя переменной в нижнем регистре: поле `site_url` читается из `SITE_URL`.
Переменные окружения важнее .env (так Docker перекрывает пути через `environment`).
Если обязательных переменных нет, бот падает при старте и перечисляет все недостающие ключи сразу.

Использование:
    from lobby_bot.settings import settings
    channel = bot.get_channel(settings.panel_channel_id)
"""

from typing import Annotated

from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Список ID через запятую: "1,2,3" -> [1, 2, 3]. NoDecode отключает разбор значения как JSON
IdList = Annotated[list[int], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # пустое значение = переменная не задана
        str_strip_whitespace=True,
        extra="ignore",  # в .env есть ключи других программ (TELEGRAM_TOKEN)
        frozen=True,  # настройки не меняются во время работы
    )

    # --- Discord и MongoDB ---
    discord_token: SecretStr
    mongo_url: SecretStr

    # --- Выключаемые фичи (по умолчанию выключены) ---
    lobby_monitor_enabled: bool = False  # lobby_bot/features/lobbies
    tournament_enabled: bool = False  # lobby_bot/features/tournament
    graphs_enabled: bool = False  # lobby_bot/features/graphs: полный граф игроков
    graphs_daily_enabled: bool = False  # lobby_bot/features/graphs: граф за сутки

    # --- Доступ к API игры (lobby_bot/features/lobbies; нужен, если LOBBY_MONITOR_ENABLED) ---
    game_header_authorization: SecretStr | None = None
    game_auth_login: str | None = None
    game_auth_password: SecretStr | None = None
    external_auth_token: SecretStr | None = None
    deployment_id: str | None = None
    nonce: str | None = None
    display_name: str | None = None

    # --- Twitch API ---
    twitch_client_id: str | None = None
    twitch_client_secret: SecretStr | None = None

    # --- Twitch: категория и уведомления ---
    twitch_monitor_enabled: bool = False  # список стримеров категории (lobby_bot/features/twitch)
    twitch_category_id: int
    twitch_streams_role_id: int
    discord_channel_id_for_twitch_notifications: int
    discord_channel_id_for_twitch_monitoring: int
    my_twitch_login: str
    my_stream_role_id: int
    my_stream_channel_id_1: int
    my_stream_channel_id_2: int

    # --- Игровые серверы (SSH) ---
    game_ssh_key_path: str
    game_server_1_host: str
    game_server_1_user: str
    game_server_1_folder: str
    game_server_2_host: str
    game_server_2_user: str
    game_server_2_folder: str
    game_server_3_host: str
    game_server_3_user: str
    game_server_3_folder: str
    # Отключённые вручную серверы (номера с 0): запуск, резерв и запрос хоста на них недоступны
    disabled_servers: IdList = []
    # Строка под «Сервера на данный момент недоступны», когда отключены все (например, условия подключения)
    servers_unavailable_note: str = ""

    # --- Веб-сервер (графики, карты матчей) ---
    site_url: str  # хранится без завершающего "/": к нему приклеиваются пути вида "/map"
    web_host: str
    web_user: str
    web_ssh_key_path: str
    web_root: str

    # --- Статистика матчей ---
    stats_csv_folder: str
    stats_csv_processed_folder: str

    # --- Discord: пользователи и роли ---
    admin_ids: IdList
    moderator_role_id: int
    extended_access_role_id: int
    hoster_role_id: int
    host_request_ping_user_id: int

    # --- Discord: каналы ---
    lobby_channel_id: int
    notify_channel_id: int
    tournament_track_channel_id: int
    panel_channel_id: int
    commands_channel_ids: IdList
    host_request_channel_id: int
    history_channel_id: int
    reservation_log_channel_id: int
    reports_log_channel_id: int
    elo_log_channel_id: int
    elo_calculator_log_channel_id: int
    match_results_channel_id: int
    penalty_log_channel_id: int
    leaderboard_channel_id: int
    duos_channel_id: int
    share_channel_id: int

    # --- Ссылки в текстах бота ---
    donation_url: str
    telegram_contact_url: str
    relicmod_download_url: str
    patcher_fix_url: str
    community_patcher_url: str
    community_reconnector_url: str

    @field_validator("admin_ids", "commands_channel_ids", "disabled_servers", mode="before")
    @classmethod
    def _split_ids(cls, value):
        if isinstance(value, str):
            return [x.strip() for x in value.split(",") if x.strip()]
        return value

    @field_validator("site_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")


def secret_value(value: SecretStr | None) -> str:
    """Значение секрета или пустая строка, если он не задан."""
    return value.get_secret_value() if value else ""


def _load() -> Settings:
    """Создать настройки. При ошибке перечислить проблемные ключи без их значений
    (стандартное сообщение pydantic показывает часть содержимого .env, а там токены)."""
    try:
        return Settings()
    except ValidationError as e:
        problems = "\n".join(
            f"  {'.'.join(str(x) for x in err['loc']).upper()}: {err['msg']}"
            for err in e.errors(include_input=False, include_url=False)
        )
        raise RuntimeError(f"Ошибка в настройках .env (см. .env.example):\n{problems}") from None


settings = _load()
