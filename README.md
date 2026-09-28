# Postgres + rabbit + pgadmin для разворачивания системы

# Информация
- rabbitmq на `localhost:5672` и management на `localhost:15672`
- postgres на `localhost:5432`
- pgadmin на `localhost:1234`
- atlassian mcp на `localhost:9000`

# Запуск
1. Переименовать .env.example -> .env
2. Заполнить все в .env
3. docker compose up -d

# Настройка jira/confluence mcp claude cli
1. docker compose up -d
2. Один раз сделать `claude mcp add --transport http atlassian http://localhost:9000/mcp --scope user`. Проверка `claude mcp list` - atlassian должно быть connected
