# Образ Discord-бота. Сборка: docker compose build
FROM python:3.13-slim

# Часовой пояс для логов (время пользователям бот и так считает в МСК)
ENV TZ=Europe/Moscow \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости: этот слой пересобирается только при изменении requirements.txt
COPY requirements.txt .
RUN pip install -r requirements.txt

# Затем код (что не попадает в образ, см. .dockerignore)
COPY . .

CMD ["python", "-m", "lobby_bot"]
