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

Тулы:
- `ask`, `summarize_files` - синхронно, ждут ответ (Claude Code);
- `ask_async`, `summarize_files_async` + `job_result`, `list_jobs` - для клиентов с таймаутом ~60 с на вызов. Задачи живут в памяти, рестарт контейнера их теряет;
- `jira_digest`, `jira_search_digest` (JQL), `confluence_digest` - квен сам читает Jira/Confluence (только GET, токены из `.env`) и отдаёт суть; `background=true` - вернуть job_id;
- `llm_health` - модели, контекст, текущие настройки.

Сервер stateless: клиенты не теряют подключение при пересборке контейнера.

Проверка: `docker exec mcp-llm python /workspace/SciMarkEnv/llm-mcp/smoke_test.py`.
