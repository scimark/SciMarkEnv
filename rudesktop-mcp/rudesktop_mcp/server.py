"""MCP-сервер: удалённый PowerShell на серверах клиентов через RuDesktop --shell."""
import asyncio
import atexit
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Literal, Optional

from mcp.server.fastmcp import FastMCP

from . import audit, dialogs
from .exe_locator import ExeNotFound
from .policy import Mode, is_read_only, needs_confirmation
from .session import RuDesktopSession, SessionError

MAX_OUTPUT = int(os.environ.get("RUDESKTOP_MAX_OUTPUT", "60000"))
MAX_TIMEOUT = 600

mcp = FastMCP(
    "rudesktop",
    instructions=(
        "Удалённый PowerShell на серверах клиентов через RuDesktop. "
        "connect() открывает сессию: ID и пароль пользователь вводит сам в локальном окне, "
        "в чате их не спрашивай. Команды однострочные, склеивай через ';'. "
        "Состояние (переменные, текущая папка) живёт между run в одной сессии. "
        "Шелл на той стороне часто работает от SYSTEM, поэтому без нужды ничего не меняй. "
        "Если пользователь отклонил команду или пароль неверный, не повторяй сам, спроси его. "
        "Закрывай сессию через close, когда закончил."
    ),
)

_sessions: dict[str, dict] = {}
# Tk не любит, когда окна создаются из разных потоков, поэтому все окошки в одном потоке
_ui = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ui")


def _ui_call(fn, *args):
    return _ui.submit(fn, *args).result()


async def _ui_async(fn, *args):
    return await asyncio.wrap_future(_ui.submit(fn, *args))


def _default_mode() -> Mode:
    try:
        return Mode(os.environ.get("RUDESKTOP_MODE", "confirm").lower())
    except ValueError:
        return Mode.CONFIRM


def _get(session_id: str) -> dict:
    entry = _sessions.get(session_id)
    if not entry:
        raise ValueError(f"Нет сессии {session_id}. Открытые: {list(_sessions) or 'нет'}")
    return entry


def _drop(session_id: str) -> None:
    entry = _sessions.pop(session_id, None)
    if entry:
        entry["session"].close()


def _trim(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    half = MAX_OUTPUT // 2
    cut = len(text) - MAX_OUTPUT
    return f"{text[:half]}\n\n... [вырезано {cut} символов, сузь команду] ...\n\n{text[-half:]}"


async def _approve_mode(target_id: str, current: Optional[Mode], wanted: Mode) -> bool:
    """Повышение режима (меньше подтверждений) только через окошко у пользователя."""
    baseline = current or _default_mode()
    if wanted.rank <= baseline.rank:
        return True
    return await _ui_async(
        dialogs.ask_confirm,
        "RuDesktop: режим",
        f"Разрешить режим «{wanted.value}» для {target_id}?",
        {
            Mode.SMART: "smart: команды, которые только читают, пойдут без подтверждения.\n"
                        "Всё остальное по-прежнему через окошко.",
            Mode.AUTO: "auto: ВСЕ команды без подтверждения, включая удаление и остановку служб.",
        }[wanted],
        "Разрешить",
    )


@mcp.tool()
async def connect(target_id: Optional[str] = None,
                  mode: Optional[Literal["confirm", "smart", "auto"]] = None) -> dict:
    """Подключиться к серверу через RuDesktop и открыть удалённый PowerShell.

    target_id: ID RuDesktop. Обычно не передавай: пользователь сам введёт ID и пароль
               в локальном окне. Если он назвал ID в чате, передай, поле будет заполнено.
    mode: confirm - каждая команда через окошко подтверждения у пользователя;
          smart - read-only команды сами, остальное через окошко; auto - без подтверждений.
          По умолчанию RUDESKTOP_MODE (confirm). Режим выше дефолта пользователь подтверждает в окошке.
    ID и пароль в чате не спрашивай.
    """
    prefill = dialogs.normalize_id(target_id) if target_id else None
    creds = await _ui_async(dialogs.ask_credentials, prefill)
    if creds is None:
        audit.log("connect_cancelled", target=prefill)
        return {"connected": False, "error": "Пользователь отменил подключение"}
    target_id, password = creds
    del creds

    wanted = Mode(mode) if mode else _default_mode()
    if not await _approve_mode(target_id, None, wanted):
        audit.log("mode_denied", target=target_id, mode=wanted.value)
        return {"connected": False, "error": f"Пользователь не разрешил режим {wanted.value}"}

    try:
        session = RuDesktopSession(target_id)
    except ExeNotFound as e:
        return {"connected": False, "error": str(e)}

    def give_password(_tid: str) -> Optional[str]:
        # пароль отдаём один раз и сразу забываем
        nonlocal password
        value, password = password, None
        return value

    try:
        greeting = await asyncio.to_thread(session.connect, give_password)
    except SessionError as e:
        audit.log("connect_failed", target=target_id, error=str(e))
        return {"connected": False, "error": str(e)}
    finally:
        password = None

    _sessions[session.session_id] = {
        "session": session, "target_id": target_id, "mode": wanted,
        "opened_at": datetime.now().isoformat(timespec="seconds"),
    }
    audit.log("connect", session=session.session_id, target=target_id, mode=wanted.value)
    return {"connected": True, "sid": session.session_id, "target_id": target_id,
            "mode": wanted.value, "greeting": greeting}


# Параметр сессии в тулзах называется sid, не session_id: мост Claude desktop
# (remote-devices) вырезает session_id из аргументов как своё служебное поле.
@mcp.tool()
async def run(sid: str, command: str, timeout: int = 60) -> dict:
    """Выполнить однострочную PowerShell-команду в сессии.

    Возвращает output (stdout+stderr текстом), exit_code ($LASTEXITCODE), success ($?).
    Если команда не успела за timeout секунд (максимум 600), вернётся done=false,
    дочитывай через read. Новую команду до завершения старой отправить нельзя.
    denied=true значит пользователь отклонил команду: не повторяй её, спроси его.
    """
    entry = _get(sid)
    session: RuDesktopSession = entry["session"]
    mode: Mode = entry["mode"]
    target = entry["target_id"]

    if needs_confirmation(mode, command):
        approved = await _ui_async(
            dialogs.ask_confirm,
            f"RuDesktop: {target}",
            f"Выполнить на {target}?  [режим {mode.value}]",
            command,
        )
        if not approved:
            audit.log("denied", session=sid, target=target, mode=mode.value, command=command)
            return {"denied": True, "message": "Пользователь отклонил команду"}
        decision = "approved"
    else:
        decision = "auto" if mode is Mode.AUTO else "read_only"

    try:
        result = await asyncio.to_thread(session.run, command, min(max(timeout, 1), MAX_TIMEOUT))
    except SessionError as e:
        if not session.alive:
            _sessions.pop(sid, None)
        audit.log("run_failed", session=sid, target=target, command=command, error=str(e))
        return {"error": str(e), "session_alive": session.alive}

    audit.log("run", session=sid, target=target, mode=mode.value, decision=decision,
              command=command, exit_code=result.get("exit_code"), done=result.get("done"))
    result["output"] = _trim(result["output"])
    return result


@mcp.tool()
async def read(sid: str, timeout: int = 30) -> dict:
    """Дочитать результат команды, которая вернула done=false."""
    session: RuDesktopSession = _get(sid)["session"]
    try:
        result = await asyncio.to_thread(session.read, min(max(timeout, 1), MAX_TIMEOUT))
    except SessionError as e:
        if not session.alive:
            _sessions.pop(sid, None)
        return {"error": str(e), "session_alive": session.alive}
    result["output"] = _trim(result["output"])
    return result


@mcp.tool()
async def set_mode(sid: str, mode: Literal["confirm", "smart", "auto"]) -> dict:
    """Сменить режим подтверждений. Повышение пользователь подтверждает в окошке."""
    entry = _get(sid)
    wanted = Mode(mode)
    if not await _approve_mode(entry["target_id"], entry["mode"], wanted):
        audit.log("mode_denied", session=sid, target=entry["target_id"], mode=wanted.value)
        return {"changed": False, "mode": entry["mode"].value, "error": "Пользователь не разрешил"}
    entry["mode"] = wanted
    audit.log("set_mode", session=sid, target=entry["target_id"], mode=wanted.value)
    return {"changed": True, "mode": wanted.value}


@mcp.tool()
async def check_command(command: str) -> dict:
    """Показать, считает ли режим smart команду read-only (ничего не выполняет)."""
    return {"read_only": is_read_only(command)}


@mcp.tool()
async def close(sid: str) -> dict:
    """Закрыть сессию."""
    entry = _get(sid)
    await asyncio.to_thread(_drop, sid)
    audit.log("close", session=sid, target=entry["target_id"])
    return {"closed": True}


@mcp.tool()
async def list_sessions() -> list[dict]:
    """Открытые сессии."""
    return [
        {"sid": sid, "target_id": e["target_id"], "mode": e["mode"].value,
         "opened_at": e["opened_at"], "alive": e["session"].alive}
        for sid, e in _sessions.items()
    ]


@atexit.register
def _cleanup() -> None:
    for sid in list(_sessions):
        _drop(sid)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
