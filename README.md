# SciMarkEnv

Локальное окружение SciMark: Postgres, RabbitMQ, pgAdmin и MCP-серверы для Claude
(Jira/Confluence, Jenkins, GitHub, командный LLM).

## Запуск

1. `copy .env.example .env` и заполнить.
2. `start.bat` / `stop.bat` (`docker compose up -d` / `down`).

Все MCP слушают только `127.0.0.1`. Команды подключения к Claude Code - в комментариях `docker-compose.yml`.

| Сервис        | Порт  |
|---------------|-------|
| postgres      | 5432  |
| rabbitmq      | 5672, 15672 |
| pgadmin       | 1234  |
| mcp-atlassian | 9000  |
| mcp-jenkins   | 9887  |
| mcp-github    | 8082  |
| mcp-llm       | 8765  |

## mcp-llm

Свой MCP-сервер (`llm-mcp/`), через который Claude делегирует простые задачи командному LLM.
Папка `WORKSPACE_DIR` смонтирована в контейнер только на чтение.
После правок кода: `docker compose up -d --build mcp-llm`.
