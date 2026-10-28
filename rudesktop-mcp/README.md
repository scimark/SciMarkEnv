# rudesktop-mcp

MCP-сервер для удалённого PowerShell на серверах клиентов через `rudesktop.exe --shell <ID>`.
Команды и вывод идут текстом, без скриншотов и окон RuDesktop.

Работает нативно на Windows. В Docker не запускается: нужны `rudesktop.exe`, WinPTY и окна на рабочем столе.

## Как устроено

- `rudesktop --shell <ID>` запускается под WinPTY. Под ConPTY RuDesktop не открывает удалённую консоль.
- Пароль вводится в локальном окне при каждом `connect`. В ответы тулз и логи он не попадает.
- Вывод команды на удалённой стороне собирается в строку, кодируется в base64 и печатается между маркерами. Экран WinPTY полон мусора от перерисовки, так он не мешает.
- Команды выполняются через dot-source: переменные и текущая папка живут между `run` в одной сессии.

## Режимы

| режим | поведение |
|---|---|
| `confirm` | каждая команда через окно подтверждения |
| `smart` | read-only команды без вопросов, остальное через окно |
| `auto` | всё без подтверждения |

Дефолт берётся из `RUDESKTOP_MODE`, по умолчанию `confirm`. Режим выше дефолта (в `connect` или `set_mode`) подтверждается в окне, понизить можно без вопросов.

Классификатор `smart` параноидальный: если не уверен, что команда только читает, спрашивает. Проверить команду без выполнения можно через тулзу `check_command`.

## Тулзы

| тулза | что делает |
|---|---|
| `connect(target_id, mode?)` | открыть сессию, пароль в окне |
| `run(session_id, command, timeout=60)` | однострочная команда, вернёт `output`, `exit_code`, `success` |
| `read(session_id, timeout=30)` | дочитать команду, вернувшую `done: false` |
| `set_mode(session_id, mode)` | сменить режим |
| `check_command(command)` | read-only или нет по мнению `smart` |
| `close(session_id)` | закрыть сессию |
| `list_sessions()` | открытые сессии |

## Установка

```bat
cd C:\Scimark\SciMarkEnv\rudesktop-mcp
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

В `%APPDATA%\Claude\claude_desktop_config.json`:

```json
"rudesktop": {
  "command": "C:\\Scimark\\SciMarkEnv\\rudesktop-mcp\\.venv\\Scripts\\python.exe",
  "args": ["-m", "rudesktop_mcp.server"],
  "env": {
    "PYTHONPATH": "C:\\Scimark\\SciMarkEnv\\rudesktop-mcp",
    "PYTHONIOENCODING": "utf-8",
    "RUDESKTOP_MODE": "confirm"
  }
}
```

## Переменные окружения

| переменная | по умолчанию | |
|---|---|---|
| `RUDESKTOP_EXE` | автопоиск | путь к `rudesktop*.exe`. Без неё ищется в Program Files, LocalAppData, Desktop, Downloads, берётся самая свежая версия |
| `RUDESKTOP_MODE` | `confirm` | режим по умолчанию |
| `RUDESKTOP_MAX_OUTPUT` | `60000` | лимит символов вывода, середина вырезается |
| `RUDESKTOP_AUDIT_LOG` | `%LOCALAPPDATA%\rudesktop-mcp\audit.jsonl` | аудит команд и решений, без вывода |
| `RUDESKTOP_PTY` | `winpty` | `conpty` для экспериментов |

## Ограничения

- Команды однострочные, склеивать через `;`.
- У долгих команд вывод приходит целиком по завершении, стриминга нет.
- Если сессию оборвало (обрыв, лимит времени), `run` вернёт ошибку, нужен новый `connect`.
- Сессии, убитые без `exit`, какое-то время висят на той стороне, и новый `connect` может упасть с `Connect to console error`. Повторить через минуту.

## Проверка

```bat
.venv\Scripts\python -m pytest
.venv\Scripts\python smoke_test.py <ID> smart
```
