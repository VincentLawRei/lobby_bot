# Архитектура бота

Карта кода: что где лежит и как фичи связаны между собой. Правила, по которым
пишется код, в `CLAUDE.md`; здесь только устройство. Детали конкретного контракта
смотри в коде (docstring модуля и функции): если описание здесь расходится с кодом,
это ошибка, которую надо исправить в одном из мест.

## Ядро (`lobby_bot/`)
- `app.py`: `LobbyBot` и экземпляр `bot`, `on_ready` (лог входа; только то, что безопасно повторять при переподключении). `setup_hook` (один раз за процесс): асинхронный клиент MongoDB `bot.db`, расширения из `EXTENSIONS`,
  синхронизация slash-команд. `close` закрывает клиент MongoDB. Всё, что требует кеша каналов (панель серверов, задачи), фичи запускают сами.
- `__main__.py`: точка входа `python -m lobby_bot`: настройка логов до остальных импортов, `run()`.
- `db.py`: класс `Database` (асинхронный клиент и коллекции; создаёт `LobbyBot.setup_hook`). Новые коллекции
  добавлять в `Database`.
- `settings.py`: типизированные настройки (pydantic-settings): класс `Settings` и объект `settings`.
- `logging_setup.py`: логирование: консоль + `logs/bot.log` в корне проекта (ротация 5 МБ x 5).

## Фичи (`lobby_bot/features/<имя>/`)
- `lobby_bot/features/servers/`: игровые серверы. `cog.py` (`ServersCog`): `!gamepanel`, `/srv`, `/stop`, `/relicmod`,
  `/useful_things`, `/usermodifiers`, `/crew_win`, `/traitors_win`; после первого on_ready создаёт панель, если её нет,
  и запускает задачи. `views.py`: панель (`ServerPanelView`, persistent, регистрируется в `setup()`), меню запуска
  (`ServerSettingsView` и селекты), остановка, резервы, инфо о сервере, принудительное завершение матча,
  `update_panel_embed`, проверки ролей по `discord.Interaction`. Кнопки консоли открывают меню фичи console.
  Без Discord: `service.py` константы
  (SERVERS, карты, моды, RANKED_MODS, `DISABLED_SERVERS`, `SERVERS_UNAVAILABLE_MESSAGE`, ID ролей и каналов),
  правила (`is_ranked_match`, `build_game_command`, ротация портов, uptime), хосты/резервы/пресеты/модификаторы,
  ELO-кеш и веса предателей для серверов; `api.py` SSH/SFTP (`SSH_CONFIGS`, `execute_ssh_command`, загрузка файлов);
  `repo.py` коллекции `discord_hosts_notify`, `discord_hosts_presets`, `discord_panel`, `game_matches`.
  Асинхронная: функции service с базой принимают `ServersRepo` первым аргументом. `ServersCog` держит
  `repo` и `console_repo` (снятие доступа к консоли при остановке), views берут их через
  `interaction.client.get_cog("ServersCog")`. SSH и загрузка ELO-кеша идут в потоке.
  Тесты: сценарии (`ui.run_scenario`) и правила сверяются с эталонами в `tests/features/servers/snapshots/`
  (SSH подменён фейком, в эталонах команды и загрузки; Discord-часть: `test_cog.py`, `test_views.py` с фейками
  из `ui.py`).
- `lobby_bot/features/console/`: консоль серверов: MinHealth, телепорт (свои, сохранённые и глобальные точки),
  выдача предметов, восстановление, броадкаст, доступ от хоста, шаринг точки в `SHARE_CHANNEL_ID` (persistent
  кнопка `shareloc:`). Своих команд нет, открывается кнопками панели (`ConsoleServerSelectView`,
  `ConsoleAccessServerSelectView`). `service.py`: команда в консоль по SSH-туннелю, доступ, точки; `repo.py`:
  `console_access`, `player_locations`. Асинхронная: `ConsoleRepo` передаётся во views, кнопки и модальные окна
  явно, views с данными при создании строятся фабрикой `create()`; persistent-кнопка шаринга берёт репозиторий
  у `ConsoleCog`. Тесты: эталоны в `tests/features/console/snapshots/`.
- `lobby_bot/features/reports/`: `/report` и общее для репортов: причины `REPORT_REASONS`, `save_report` (репорт
  из итогов матча тоже сохраняет он, репозиторий передаёт вызывающий). Лог в `REPORTS_LOG_CHANNEL_ID`; если сезон
  не читается: сезон 1, как было. Тесты: эталоны в `tests/features/reports/snapshots/`.
- `lobby_bot/features/match_stats/`: импорт файлов матчей раз в минуту (`import_task`; MongoDB асинхронно, файлы и SFTP в потоке):
  `parser.py` разбор CSV/JSON (чистые функции), `importer.py` порядок импорта и перенос в processed,
  `site.py` карта матча на сайте, `repo.py` статистика, очередь ELO, урон. Валидные нерейтинговые
  матчи уходят событием `casual_matches_imported`.
- `lobby_bot/features/elo/`: ядро ELO: `service.py` расчёт матча (`calculate_match`) и decay (`decay_rating`)
  чистыми функциями, калибровка, винстрики, штрафы, твинки, ручные бонусы (`elo_overrides()`, их читает
  servers для ELO-кеша); `repo.py` рейтинги, история, очередь. Функции service с базой асинхронные, первым
  аргументом `EloRepo` (собирается в `setup()` из `bot.db`). В cog два Cog: `EloCog`: задачи `elo_processing_task` (после новых
  матчей событие `elo_matches_processed`), `penalty_expiry_task` (снятие штрафов по сроку, 5 мин) и
  `daily_elo_decay_task` (пост в `ELO_CALCULATOR_LOG_CHANNEL_ID`); `EloAdminCog`: `/elo_adjust`,
  `/elo_adjust_group`, `/elo_adjust_group_certain`, `/link_twink`, `/unlink_twink`, `/elo_revert_match`, `/elo_penalty`,
  `/elo_penalty_remove` (лог в `ELO_LOG_CHANNEL_ID`; права как было: только роль модератора, без `ADMIN_IDS`;
  если сезон не читается: сезон 1, как было). Тесты: эталоны в `tests/features/elo/snapshots/`
  (`test_replay.py`: всё, что пишет расчёт синтетического сезона и decay; `test_admin.py`: команды модераторов).
- `lobby_bot/features/leaderboard/`: лидерборд: сообщения в каналах `LEADERBOARD_CHANNEL_ID` (таблица, кнопки
  `FullTableButton`) и `DUOS_CHANNEL_ID` (статистика Traitors), `leaderboard_update_task` (5 мин) и слушатель
  `elo_matches_processed`; `/leaderboard`, `/player`, `/change_rating_nickname` (только роль модератора, без
  `ADMIN_IDS`, как было), `/season_leaderboard`, `/season_stats`, `/season_player`. `service.py`: расчёты
  (дуэты, серии, топ WR, вечный мирный), строки таблицы, график ELO; `cog.py`: embed'ы, кнопки, команды.
  Репозиторий собирает `setup()` и передаёт в Cog и persistent `FullTableButton`; график рисуется в потоке.
  Тесты: эталоны embed'ов в `tests/features/leaderboard/snapshots/`.
- `lobby_bot/features/match_results/`: итоги матчей в `MATCH_RESULTS_CHANNEL_ID` (рейтинговые с ELO и
  нерейтинговые), штрафы и твинки матча в `PENALTY_LOG_CHANNEL_ID`, статистика урона, persistent меню
  детальной статистики (`stats:<match>`) и кнопка репорта (`report:<match>`, репорты в `player_reports_col`).
  Слушает `elo_matches_processed` и `casual_matches_imported`. Известная проблема с матчами твинков: см.
  «Известные проблемы» в `CLAUDE.md`. Репозитории (свой и `ReportsRepo`) держит `MatchResultsCog`; persistent
  меню и кнопка, созданные Discord после рестарта, берут их у Cog через `interaction.client`.
  Тесты: эталоны в `tests/features/match_results/snapshots/`.
- `lobby_bot/features/roles/`: роли по реакциям в #roles и хосты (роль со `steam_id`): реакции,
  поиск сообщения «Выбор роли» при старте, `!createmulti`, `!addhost`, `!removehost`, `!setrolesmsg`,
  `!setdesc`, `!showrolesmsg`. Коллекции `roles_and_emojis` и `bot_state` (ключ `last_roles_msg_id`).
- `lobby_bot/features/players/`: `!search`, `!nick`, `!twitch` (коллекция `players`).
- `lobby_bot/features/dm_log/`: личные сообщения боту сохраняются в `dm_logs`, автору приходит ответ.
- `lobby_bot/features/seasons/`: `current_season(seasons)` и `season_or_first(seasons)` (service.py) и
  `/reset_season`. `seasons_repo(bot.db)` собирает SeasonsRepo: его получают фичи, которым нужен сезон (elo,
  graphs и leaderboard через свой repo, match_stats, reports, servers).
- `lobby_bot/features/twitch/`: Twitch-оповещения (общий функционал): `my_stream_task`: стрим владельца
  с пингом роли; `monitor_task`: стримеры категории (включается `TWITCH_MONITOR_ENABLED=true`).
- `lobby_bot/features/lobbies/`: мониторинг лобби (`LOBBY_MONITOR_ENABLED`): `api.py` лобби из EOS API,
  доска лобби, уведомления хостов (пинг не чаще 45 мин), игроки в `players`, снапшоты в `lobbies`.
- `lobby_bot/features/graphs/`: графы игроков на сайте (`GRAPHS_ENABLED`, `GRAPHS_DAILY_ENABLED`): `full.py`
  полный граф (чистые функции), `overlay.html` интерфейс поверх графа (подстановки `__ИМЯ__`),
  `daily.py` граф за сутки. HTML собирается в памяти и загружается по SFTP, локальных файлов нет.
- `lobby_bot/features/tournament/`: список участников турнира (`TOURNAMENT_ENABLED`).
- `telegram_polls/`: отдельный Telegram-бот опросов, с Discord-ботом не связан
  (`python telegram_polls/polls_bot.py`).

## События между фичами
Фичи не вызывают Discord-часть друг друга напрямую: событие `self.bot.dispatch("имя", данные)`,
слушатель `@commands.Cog.listener() on_имя`.

| Событие | Кто отправляет | Кто слушает | Данные |
|---|---|---|---|
| `elo_matches_processed` | elo | leaderboard, match_results | список (match_uuid, season) |
| `casual_matches_imported` | match_stats | match_results | нерейтинговые матчи |

## Фоновые задачи (`@tasks.loop`)
- Задачи фич-Cogs запускаются в самих Cogs: `twitch` (`my_stream_task`; `monitor_task` по флагу),
  `match_stats` (`import_task`, раз в минуту), `elo` (`elo_processing_task` и `penalty_expiry_task` 5 мин,
  `daily_elo_decay_task` 6 ч), `leaderboard` (`leaderboard_update_task`, 5 мин),
  `lobbies`, `graphs`, `tournament` (по флагам `*_ENABLED`, по умолчанию выключены),
  `servers` (после первого on_ready: `panel_update_task` 5 мин, `auto_stop_task` 5 мин,
  `reservation_cleanup_task` 1 мин, `failed_start_check_task` 1 мин).

Список запущенных задач проверяет `tests/test_startup.py`.

## Persistent-кнопки и меню
Регистрируются в `setup()` фичи (`add_view` / `add_dynamic_items`): `FullTableButton` в leaderboard,
`PlayerSelectMenu` и `ReportMatchButton` в match_results, `ShareLocationButton` в console,
`ServerPanelView` в servers. Список проверяет `tests/test_startup.py`.

## Правила предметной области
- Ranked-матч (`is_ranked_match` в `lobby_bot/features/servers/service.py`): режим `normal`, не islands/outpost,
  без `chill`, все моды из `RANKED_MODS` (или без модов).
- ELO: матч попадает в очередь `elo_processing`, обрабатывается `elo_processing_task`. Расчёт сначала
  сохраняется в документ очереди (`elo_plan`), потом пишется; после сбоя дописывается сохранённый расчёт
  (`process_queued_match`). Запись `elo_history` за матч одна на (match_uuid, steam_id, season).
- Отключение серверов: `DISABLED_SERVERS` в `.env` (номера с 0 через запятую). Если отключены все,
  `/srv` и кнопки панели показывают `SERVERS_UNAVAILABLE_MESSAGE` (строка под ним: `SERVERS_UNAVAILABLE_NOTE`).
- Ручные бонусы веса на предателя: коллекция `elo_overrides` ({steam_id, bonus}), правятся в MongoDB.
