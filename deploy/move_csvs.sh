#!/bin/bash
# Переносит загруженные файлы статистики матчей в папку, которую читает бот (STATS_CSV_FOLDER).
# Установка и cron: docs/SERVER.md
mv /srv/uploads/*.csv /srv/bot/csvs/ 2>/dev/null
mv /srv/uploads/*.json /srv/bot/csvs/ 2>/dev/null
