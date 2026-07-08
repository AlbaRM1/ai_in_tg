# Universal LLM Telegram Bot

Модульный production-ready Telegram бот для работы с различными LLM через OpenAI-совместимые эндпоинты. Каждый пользователь подключает свой собственный API endpoint.

## 🎯 Основные возможности

- ✅ **Multi-user**: каждый пользователь использует свой LLM endpoint и API ключ
- ✅ **Token-based Registration**: безопасная регистрация через одноразовые токены
- ✅ **Settings Management**: полное управление настройками через FSM в приватном чате
- ✅ **Streaming**: real-time генерация ответов с typing-индикатором
- ✅ **Forum Topics**: каждый топик = отдельная сессия чата (как "New Chat" в ChatGPT)
- ✅ **Multimodal Support**: 
  - Изображения (фото и document-картинки)
  - Документы (PDF, текстовые файлы)
  - Текст с caption
- ✅ **Smart Context Management**: 
  - Sliding window с лимитом 200k токенов
  - Автоматическое сжатие истории
  - Model-aware token counting через `litellm`
- ✅ **Rich Formatting**: 
  - HTML parse_mode
  - Expandable blockquotes для thinking-процесса модели
  - Code blocks с подсветкой
- ✅ **Production-ready**:
  - Async everywhere (aiogram 3.x, SQLAlchemy 2.0, asyncpg)
  - Structured logging
  - Error handling (timeouts, rate limits)
  - Encryption для API ключей (Fernet-ready)
  - Middleware архитектура (Database + Auth)

## 📋 Требования

- Python 3.12+ (проект протестирован на 3.14, но для продакшена рекомендуется 3.12)
- PostgreSQL 14+
- Telegram Bot Token (от [@BotFather](https://t.me/BotFather))

## 🚀 Быстрый старт

### 1. Клонирование и установка зависимостей

```bash
# Клонируйте репозиторий (или используйте текущую директорию)
cd ai_in_tg

# Активируйте виртуальное окружение
source .venv/bin/activate

# Зависимости уже установлены, но если нужно переустановить:
# pip install -r requirements.txt
```

### 2. Автонастройка базы данных

**Простой способ (рекомендуется):**

Используйте скрипт автоматической настройки PostgreSQL:

```bash
./setup_db.sh
```

Скрипт автоматически:
- Проверит установку и запуск PostgreSQL
- Создаст пользователя `ai_in_tg_user` и базу `ai_in_tg`
- Сгенерирует безопасный пароль
- Обновит `DATABASE_URL` в файле `.env`

**Кастомизация параметров:**

```bash
# Переопределить имя базы и пользователя
DB_NAME=mybot DB_USER=myuser ./setup_db.sh

# Задать собственный пароль
DB_PASSWORD=secret ./setup_db.sh
```

**Ручная настройка (альтернатива):**

Если предпочитаете настраивать вручную:

```bash
# Создайте базу данных
sudo -u postgres psql
postgres=# CREATE DATABASE llm_bot;
postgres=# CREATE USER llm_user WITH PASSWORD 'your_secure_password';
postgres=# GRANT ALL PRIVILEGES ON DATABASE llm_bot TO llm_user;
postgres=# \q
```

### 3. Конфигурация

Скопируйте `.env.example` в `.env` и заполните:

```bash
cp .env.example .env
nano .env
```

Минимальная конфигурация:

```env
BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz  # От @BotFather
ADMIN_TELEGRAM_ID=123456789                       # Ваш Telegram ID
DATABASE_URL=postgresql+asyncpg://llm_user:your_secure_password@localhost:5432/llm_bot
MAX_CONTEXT_TOKENS=200000                         # Опционально (по умолчанию 200k)
```

### 4. Запуск

```bash
python main.py
```

При первом запуске автоматически создадутся таблицы БД.

## 🛠️ Использование

### Для администратора

1. **Генерация токена регистрации**:
   - Отправьте боту `/generate_token` в личке
   - Получите одноразовый токен
   - Передайте токен пользователю

### Для пользователя

1. **Регистрация**:
   - Отправьте боту `/start <токен>` в личке
   - Токен используется один раз и становится неактивным

2. **Настройка эндпоинта**:
   - Отправьте `/settings` в личке с ботом
   - Выберите "🔌 Мои эндпоинты" → "➕ Добавить эндпоинт"
   - Введите название (например, "OpenAI")
   - Введите URL (например, `https://api.openai.com`)
   - Введите API ключ (он будет зашифрован)
   - Бот автоматически проверит эндпоинт и получит список моделей
   - Активируйте эндпоинт в списке

3. **Выбор модели**:
   - В `/settings` выберите "🤖 Выбор модели"
   - Выберите модель из списка
   - Можете добавить модели в избранное (⭐)

4. **Использование в топиках**:
   - Создайте **supergroup** в Telegram
   - В настройках группы включите **Topics** (форум-режим)
   - Добавьте бота в группу и дайте права администратора
   - Создайте топики для разных чатов
   - Пишите в топиках — бот ответит со streaming
   - Поддерживаются: текст, изображения (фото), документы (PDF, txt, etc.)
   - Каждый топик = отдельная история (до 200k токенов)

## 📁 Структура проекта

```
ai_in_tg/
├── .env                          # Конфигурация (не в git)
├── .env.example                  # Шаблон конфигурации
├── requirements.txt              # Python зависимости
├── main.py                       # Точка входа
└── app/
    ├── config/
    │   └── settings.py           # Pydantic settings
    ├── database/
    │   ├── base.py               # Engine, session factory
    │   └── models.py             # SQLAlchemy 2.0 async models
    ├── handlers/
    │   ├── registration.py       # /start, /generate_token
    │   ├── settings.py           # /settings + FSM
    │   └── chat.py               # Хендлер топиков со streaming
    ├── middlewares/
    │   ├── database.py           # DB session middleware
    │   └── auth.py               # Authorization middleware
    ├── services/
    │   ├── llm_service.py        # LLM streaming через litellm
    │   ├── context_service.py    # Token counting, sliding window
    │   ├── user_service.py       # User & token management
    │   ├── endpoint_service.py   # Endpoint & model management
    │   └── attachment_service.py # Image/document processing
    ├── keyboards/
    │   └── inline.py             # Inline клавиатуры для /settings
    ├── states/
    │   └── settings.py           # FSM states для настроек
    └── utils/
        ├── crypto.py             # Encrypt/decrypt API ключей
        ├── formatting.py         # HTML formatting, thinking blocks
        └── typing.py             # Typing indicator task
```

## 🔧 Расширенная настройка

### Подключение шифрования API-ключей

Шифрование уже работает с заглушкой. Для настоящего шифрования:

1. Сгенерируйте Fernet ключ:

```python
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
```

2. Добавьте в `.env`:

```env
ENCRYPTION_KEY=your_fernet_key_here
```

3. Раскомментируйте код в `app/utils/crypto.py`

4. API ключи автоматически шифруются при добавлении эндпоинтов

### Настройка лимита контекста

```env
MAX_CONTEXT_TOKENS=200000  # По умолчанию 200k
```

### Логирование

Измените уровень логирования в `main.py`:

```python
logging.basicConfig(level=logging.DEBUG)  # Для отладки
```

## 🔮 TODO / Roadmap

### Планируемые улучшения

- [ ] **Alembic миграции** (сейчас используется `create_all`)
- [ ] **Docker support**: Dockerfile и docker-compose.yml
- [ ] **Webhook mode**: альтернатива polling для продакшена
- [ ] **Улучшенное сжатие контекста**
  - Summarization через быструю модель вместо простого удаления
  - Сохранение важных сообщений
- [ ] **Дополнительные команды**
  - `/new_topic` — очистка контекста топика
  - `/export` — экспорт истории чата
- [ ] **Мониторинг**
  - Rate limiting per user
  - Metrics / Prometheus
  - Логирование использования токенов

## 🐛 Troubleshooting

### Bot не отвечает в топиках

- Убедитесь, что группа в режиме forum (Topics включены)
- Бот должен быть администратором с правами на сообщения
- Пишите именно в топиках, не в General
- Проверьте, что вы зарегистрированы и настроили эндпоинт

### "Вы не зарегистрированы"

- Попросите администратора создать токен через `/generate_token`
- Отправьте боту `/start <токен>` в личке

### "Сначала настройте эндпоинт и модель"

- Отправьте `/settings` в личке с ботом
- Добавьте эндпоинт через меню
- Выберите модель

### Ошибки при валидации эндпоинта

- Проверьте корректность URL (должен начинаться с `http://` или `https://`)
- Проверьте, что API ключ действителен
- Убедитесь, что эндпоинт доступен (не блокирован файрволом)

### Ошибки совместимости Python 3.14

- Используйте Python 3.12 для стабильной работы
- Или установите зависимости с флагом: `PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 pip install ...`

### litellm ошибки токенизации

- Fallback автоматический (оценка ~4 символа = 1 токен)
- Проверьте, что модель названа правильно (например, `gpt-3.5-turbo`, а не `gpt-3.5`)

## 📄 Лицензия

MIT (или ваша лицензия)

## 🤝 Вклад

Pull requests приветствуются! Для крупных изменений сначала откройте issue для обсуждения.

## ⚡ Production Deploy

### Systemd Service

```ini
[Unit]
Description=Universal LLM Telegram Bot
After=network.target postgresql.service

[Service]
Type=simple
User=llm-bot
WorkingDirectory=/opt/ai_in_tg
Environment="PATH=/opt/ai_in_tg/.venv/bin"
ExecStart=/opt/ai_in_tg/.venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Nginx reverse proxy (опционально, для webhook)

TODO: Пока работает на polling, webhook = будущая фича

---

**Вопросы?** Откройте issue или свяжитесь с мейнтейнером.
