# Портал в Будущее

Бот для мессенджера MAX. Учитель выбирает предмет, класс и тему, бот собирает пятиминутку: PDF на лист A4 (два варианта) и сообщение с ответами.

Проект хакатона «Идея Фикс».

## Запуск из командной строки

Нужен Python 3.12.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python main.py
```

На Linux и macOS активация окружения: `source .venv/bin/activate`, файл окружения копируется так: `cp .env.example .env`.

В `.env` указываются `MAX_BOT_TOKEN` (кабинет разработчика MAX) и `GIGACHAT_CREDENTIALS` (GigaChat API). Файл `.env` в репозиторий не кладётся.

## Docker

Сборка образа и запуск контейнера из корня проекта:

```bash
docker build -t portal-v-budushee .
docker run --env-file .env portal-v-budushee
```

Образ собран на Python 3.12. Токены читаются из `.env` в момент запуска, внутрь образа они не копируются. Остановить контейнер: `Ctrl+C`.

По умолчанию бот работает через long polling. Если раньше был включён webhook, при старте он снимается. Режим webhook: `BOT_MODE=webhook` и переменные `WEBHOOK_*` в `.env`. Для MAX нужен HTTPS.

## Как устроено

| Файл | Зачем |
|------|--------|
| `main.py` | запуск |
| `bot.py` | диалог в MAX |
| `menu.py` | тексты и кнопки |
| `generator.py` | запрос к GigaChat и проверка ответов |
| `prompt_builder.py` | текст запроса |
| `pdf_builder.py` | лист A4 |
| `math_render.py` | формулы в PDF |
| `models.py` | формат карточки |
| `tasks_db.json` | темы и примеры |
| `config.py` | настройки и шкала оценок |

В одном варианте от 2 до 8 заданий. На листе всегда два варианта. Оценка: 5 от 85% верных, 4 от 65%, 3 от 45%, иначе 2.

Команды: `/start`, `/help`.
