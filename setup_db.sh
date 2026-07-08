#!/usr/bin/env bash
set -euo pipefail

# Цвета для вывода
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Настраиваемые переменные (можно переопределить через env)
DB_NAME="${DB_NAME:-ai_in_tg}"
DB_USER="${DB_USER:-ai_in_tg_user}"
DB_PASSWORD="${DB_PASSWORD:-$(openssl rand -hex 16)}"
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"

ENV_FILE=".env"

echo -e "${BLUE}=== Настройка PostgreSQL для Telegram-бота ===${NC}\n"

# Проверка наличия PostgreSQL
echo -e "${YELLOW}→${NC} Проверка установки PostgreSQL..."
if ! command -v psql &> /dev/null; then
    echo -e "${RED}✗ PostgreSQL не установлен!${NC}"
    echo -e "\nУстановите PostgreSQL командой:"
    echo -e "  ${BLUE}sudo apt update && sudo apt install -y postgresql postgresql-contrib${NC}\n"
    exit 1
fi
echo -e "${GREEN}✓${NC} PostgreSQL установлен"

# Проверка, что PostgreSQL запущен
echo -e "${YELLOW}→${NC} Проверка статуса PostgreSQL..."
if ! pg_isready -q; then
    echo -e "${RED}✗ PostgreSQL не запущен!${NC}"
    echo -e "\nЗапустите PostgreSQL командой:"
    echo -e "  ${BLUE}sudo systemctl start postgresql${NC}"
    echo -e "  ${BLUE}sudo systemctl enable postgresql${NC}  # для автозапуска\n"
    exit 1
fi
echo -e "${GREEN}✓${NC} PostgreSQL запущен"

# Проверка существования пользователя
echo -e "${YELLOW}→${NC} Проверка пользователя базы данных..."
USER_EXISTS=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" 2>/dev/null || echo "0")

if [[ "$USER_EXISTS" == "1" ]]; then
    echo -e "${GREEN}✓${NC} Пользователь '${DB_USER}' уже существует"
else
    echo -e "${YELLOW}→${NC} Создание пользователя '${DB_USER}'..."
    sudo -u postgres psql -c "CREATE ROLE ${DB_USER} WITH LOGIN PASSWORD '${DB_PASSWORD}';" > /dev/null
    echo -e "${GREEN}✓${NC} Пользователь '${DB_USER}' создан"
fi

# Проверка существования базы данных
echo -e "${YELLOW}→${NC} Проверка базы данных..."
DB_EXISTS=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" 2>/dev/null || echo "0")

if [[ "$DB_EXISTS" == "1" ]]; then
    echo -e "${GREEN}✓${NC} База данных '${DB_NAME}' уже существует"
else
    echo -e "${YELLOW}→${NC} Создание базы данных '${DB_NAME}'..."
    sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};" > /dev/null
    echo -e "${GREEN}✓${NC} База данных '${DB_NAME}' создана"
fi

# Выдача привилегий (на всякий случай, если БД уже существовала)
echo -e "${YELLOW}→${NC} Назначение привилегий..."
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};" > /dev/null
echo -e "${GREEN}✓${NC} Привилегии назначены"

# Обновление .env файла
echo -e "${YELLOW}→${NC} Обновление файла конфигурации..."

DATABASE_URL="postgresql+asyncpg://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}"

if [[ ! -f "$ENV_FILE" ]]; then
    echo -e "${YELLOW}→${NC} Создание файла ${ENV_FILE}..."
    touch "$ENV_FILE"
fi

if grep -q "^DATABASE_URL=" "$ENV_FILE"; then
    # Заменяем существующую строку
    sed -i "s|^DATABASE_URL=.*|DATABASE_URL=${DATABASE_URL}|" "$ENV_FILE"
    echo -e "${GREEN}✓${NC} DATABASE_URL обновлён в ${ENV_FILE}"
else
    # Добавляем в конец файла
    echo "DATABASE_URL=${DATABASE_URL}" >> "$ENV_FILE"
    echo -e "${GREEN}✓${NC} DATABASE_URL добавлен в ${ENV_FILE}"
fi

# Итоговая сводка
echo -e "\n${GREEN}=== Настройка завершена успешно! ===${NC}\n"
echo -e "${BLUE}Параметры базы данных:${NC}"
echo -e "  База данных:  ${GREEN}${DB_NAME}${NC}"
echo -e "  Пользователь: ${GREEN}${DB_USER}${NC}"
echo -e "  Пароль:       ${GREEN}${DB_PASSWORD}${NC}"
echo -e "  Хост:         ${GREEN}${DB_HOST}${NC}"
echo -e "  Порт:         ${GREEN}${DB_PORT}${NC}"
echo -e "\n${YELLOW}Примечание:${NC} Таблицы БД будут созданы автоматически при первом запуске бота."
echo -e "\n${BLUE}Запустите бота командой:${NC}"
echo -e "  ${GREEN}python main.py${NC}\n"
