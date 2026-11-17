# Запуск в Docker

Бот работает в контейнере, MongoDB остаётся на своём месте (подключение по `MONGO_URL` из `.env`).

## Требования

- Linux-сервер с Docker Engine и плагином Compose (`docker compose version`).
- В папке проекта лежит `.env`.
- SSH-ключи и папки CSV существуют по путям из `.env` (`GAME_SSH_KEY_PATH`, `WEB_SSH_KEY_PATH`,
  `STATS_CSV_FOLDER`, `STATS_CSV_PROCESSED_FOLDER`). Если файла или папки нет, Docker создаст на их
  месте пустую папку, и бот не сможет подключиться по SSH.

## Первый запуск

1. Остановите бота, запущенного без Docker (иначе будут работать две копии с одним токеном).
2. В папке проекта:
   ```
   docker compose up -d --build
   docker compose logs -f
   ```
3. В логах должно появиться `✅ Logged in as ...`.

## Обновление кода

```
docker compose up -d --build
```

Пересобирается только слой с кодом; зависимости пересобираются, если изменился `requirements.txt`.

## Полезные команды

| Задача | Команда |
|---|---|
| Статус | `docker compose ps` |
| Логи в реальном времени | `docker compose logs -f` |
| Перезапуск | `docker compose restart` |
| Остановка | `docker compose down` |
| Консоль внутри контейнера | `docker compose exec bot bash` |

## Важно про `.env`

Docker Compose читает `.env` сам и подставляет `$ПЕРЕМЕННЫЕ`. Если в каком-то значении (пароль, токен)
есть символ `$`, его нужно удвоить: `$$`. Иначе значение исказится.

## Откат на запуск без Docker

```
docker compose down
python -m lobby_bot
```
