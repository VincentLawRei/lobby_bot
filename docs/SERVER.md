# Сервер бота: что установлено помимо Docker

Бот запускается в Docker (см. `docs/DOCKER.md`). Здесь описано то, что живёт на сервере вне контейнера
и без чего бот не получает данные и не выкатывается. Если меняешь что-то на сервере, обнови этот файл и
копию в `deploy/`.

Пути ниже обозначены так (подставь свои):

| Обозначение | Что это | Пример |
|---|---|---|
| `$BOT_DIR` | папка бота на сервере (git-репозиторий, рядом `.env`) | `/opt/lobby_discord_bot` |
| `$UPLOAD_DIR` | куда игровые серверы загружают файлы матчей | `/srv/uploads` |
| `$STATS_CSV_FOLDER` | откуда бот читает файлы матчей (ключ в `.env`) | `/opt/lobby_data/csvs` |
| `$STATS_CSV_PROCESSED_FOLDER` | куда бот складывает обработанные (ключ в `.env`) | `/opt/lobby_data/csvs_processed` |

## Статистика матчей: `move_csvs.sh`

Файлы матчей присылают игровые серверы:
- `*.csv` со статистикой матча генерирует сама игра;
- `*.json` с данными урона формирует Node.js-скрипт на игровом сервере.

Игровые серверы загружают их в `$UPLOAD_DIR` под отдельным пользователем с ограниченными командами:
другого доступа к машине бота у них нет.

Скрипт раз в минуту переносит их в `$STATS_CSV_FOLDER`. Оттуда их забирает бот (`import_task` в
`lobby_bot/features/match_stats/`) и складывает обработанные в `$STATS_CSV_PROCESSED_FOLDER`.
В контейнер обе папки подключаются томами (`docker-compose.yml`).

```
$UPLOAD_DIR  --(move_csvs.sh, cron)-->  $STATS_CSV_FOLDER  --(бот)-->  $STATS_CSV_PROCESSED_FOLDER
```

Пути в скрипте вписаны: `/srv/uploads` (`$UPLOAD_DIR`) и `/srv/bot/csvs` (`$STATS_CSV_FOLDER`).
На другом сервере поправь их в копии скрипта.

### Установка

```bash
install -m 755 deploy/move_csvs.sh /usr/local/bin/move_csvs.sh
mkdir -p "$STATS_CSV_FOLDER" "$STATS_CSV_PROCESSED_FOLDER"
crontab -e   # добавить строку ниже
```

```
* * * * * /usr/local/bin/move_csvs.sh
```

Проверка: `crontab -l` показывает строку; положить тестовый `.csv` в `$UPLOAD_DIR`, через минуту он должен
оказаться в `$STATS_CSV_FOLDER`.

### Известные ограничения

- `mv` не знает, закончилась ли загрузка: файл, который ещё пишется в момент запуска cron,
  может быть перенесён недописанным.
- Ошибки `mv` скрыты (`2>/dev/null`), поэтому проблемы с правами или местом на диске не видны.

## Выкатка: `git push server main`

`$BOT_DIR` на сервере — git-репозиторий, в который пушат с ПК (`receive.denyCurrentBranch updateInstead`:
пуш сразу обновляет файлы в папке). Хук `post-receive` после пуша в `main`:
1. собирает образ (`docker compose build`);
2. прогоняет `pytest` во временном контейнере из этого образа на файлах коммита (`git archive`, без `.env`
   и файлов вне git; ставит `requirements-dev.txt`);
3. только при зелёных тестах выполняет `docker compose up -d`.

Вывод виден в терминале того, кто пушит. Если сборка или тесты упали, продолжает работать прежний контейнер,
а в папке бота уже лежит новый код: следующий пуш с исправлением выкатит его как обычно.

- Эталон хука: `deploy/post-receive`, на сервере лежит в `$BOT_DIR/.git/hooks/post-receive`.
- Файлы вне git (`.env`, `logs/`) пуш не трогает.
- Включить или отключить игровой сервер без коммита: `DISABLED_SERVERS` в `.env` на сервере, затем
  `docker compose up -d` (контейнер пересоздаётся с новым `.env`; `restart` его не перечитывает).
- Если на сервере изменён закоммиченный файл, пуш отклоняется (`Working directory has unstaged changes`):
  посмотреть `git status`, перенести правку в репозиторий на ПК, на сервере `git checkout -- <файл>`.
- Пуш в другие ветки ничего не выкатывает: так делаются шаги, которые надо проверить на сервере перед
  выкаткой (например, разовая проверка через `docker compose run`).

### На ПК (один раз в клоне)

```bash
git config core.hooksPath .githooks
git remote add server bot-server:/opt/lobby_discord_bot   # путь = $BOT_DIR
```

`bot-server` описан в `~/.ssh/config` (адрес, пользователь, ключ). Хук `.githooks/post-commit` отправляет
каждый коммит в `main` на сервер. Без автопуша: `git push server main` вручную.

### На сервере (один раз)

```bash
cd "$BOT_DIR"                         # .env и остальные файлы вне git уже на месте
git init -b main
# на ПК: git push server main:deploy-init
git checkout -f -B main deploy-init
git branch -D deploy-init
git config receive.denyCurrentBranch updateInstead
install -m 755 deploy/post-receive .git/hooks/post-receive
```
