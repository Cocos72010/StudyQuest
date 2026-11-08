# StudyQuest MAX-бот

StudyQuest — учебный бот для платформы MAX. Проект запускается как Docker-контейнер; SQLite-файл хранится в именованном Docker volume.

## Состав Docker-образа

- `Dockerfile` собирает образ на основе `python:3.12-slim`.
- В образ копируются `app.py` и `requirements.txt`.
- Контейнер запускает `python app.py` от непривилегированного пользователя.
- Данные SQLite сохраняются в volume `studyquest-data`, смонтированном в `/app/data`.

## Зависимости Python

Список ограничений версий библиотек находится в `requirements.txt`:

- `requests>=2.31,<3.0` — HTTP-запросы к API MAX.
- `certifi>=2023.5.7,<2027` — сертификаты доверенных центров сертификации.

## Настройка перед запуском

Создайте локальный `.env` рядом с `docker-compose.yml` и задайте значения:

```dotenv
MAX_BOT_TOKEN=<токен MAX-бота>
TEACHER_MAX_ID=<MAX ID админа>
LOG_LEVEL=INFO
```

Не добавляйте `.env`, токены или другие секреты в Git и не встраивайте их в Docker-образ. Compose автоматически использует `.env` для подстановки переменных. При необходимости корневой сертификат MAX можно поместить в `certs/ministry-root.crt`; каталог сертификатов подключается в контейнер только для чтения.

## Сборка и запуск

Соберите Docker-образ одной командой:

```sh
docker build -t studyquest-max-bot .
```

Запустите Compose-службу `max_bot` с пересборкой образа и проверкой актуальности базовых образов:

```sh
docker compose up --pull always --build -d max_bot
```

Для просмотра состояния и логов используйте:

```sh
docker compose ps
```

```sh
docker compose logs -f max_bot
```

## Запуск контейнера напрямую

После сборки образа и настройки `.env` контейнер можно запустить одной командой:

```sh
docker run -d --name max_bot --restart unless-stopped --env-file .env -e DATABASE_PATH=/app/data/school.db -e MAX_CA_CERT_FILE=/certs/ministry-root.crt -v studyquest-data:/app/data -v "${PWD}/certs:/certs:ro" studyquest-max-bot
```

Эта команда рассчитана на POSIX-совместимую оболочку. При использовании PowerShell замените `${PWD}` на полный путь к каталогу `certs`.
