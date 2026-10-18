"""MCP-сервер: делегирование задач командному LLM через OpenAI-совместимый API."""
import glob
import os
from pathlib import Path, PureWindowsPath

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
API_KEY = os.environ.get("LLM_API_KEY", "")
MODEL = os.environ.get("LLM_MODEL", "")
TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "900"))
# Лимитов нет - по умолчанию думаем на максимум.
THINKING = os.environ.get("LLM_THINKING", "true").lower() == "true"
REASONING_EFFORT = os.environ.get("LLM_REASONING_EFFORT", "xhigh")

# Файлы: хостовая папка (как её видит пользователь) смонтирована в WORKSPACE.
WORKSPACE = Path("/workspace")
HOST_PREFIX = PureWindowsPath(os.environ.get("WORKSPACE_HOST_PREFIX", r"C:\Scimark"))
# ~3 символа на токен, контекст 393k - оставляем запас под рассуждения и ответ.
MAX_INPUT_CHARS = int(os.environ.get("LLM_MAX_INPUT_CHARS", "800000"))
MAX_FILES = int(os.environ.get("LLM_MAX_FILES", "200"))

mcp = FastMCP("llm", host="0.0.0.0", port=int(os.environ.get("MCP_PORT", "8765")))


def _client() -> httpx.AsyncClient:
    if not BASE_URL or not API_KEY:
        raise RuntimeError("LLM_BASE_URL / LLM_API_KEY не заданы в .env")
    return httpx.AsyncClient(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=TIMEOUT,
    )


async def _chat(messages: list[dict]) -> str:
    """Один запрос к модели. Рассуждения не возвращаем - только итоговый ответ."""
    payload = {
        "model": MODEL or None,
        "messages": messages,
        "thinking": THINKING,
        "reasoning_effort": REASONING_EFFORT,
        "include_reasoning": False,
    }
    async with _client() as client:
        resp = await client.post("/chat/completions", json=payload)
        resp.raise_for_status()
    return (resp.json()["choices"][0]["message"]["content"] or "").strip()


def _to_container(path: str) -> str:
    """C:\\Scimark\\x\\*.log -> /workspace/x/*.log. Пути внутри /workspace оставляем как есть."""
    if path.startswith(str(WORKSPACE)):
        return path
    win = PureWindowsPath(path)
    try:
        rel = win.relative_to(HOST_PREFIX)
    except ValueError:
        raise ValueError(f"Путь вне {HOST_PREFIX}: {path}") from None
    return str(WORKSPACE.joinpath(*rel.parts))


def _resolve_files(paths: list[str]) -> list[Path]:
    """Разворачивает глобы, отсекает всё, что вне /workspace, и дубликаты."""
    files: dict[Path, None] = {}
    for raw in paths:
        for match in sorted(glob.glob(_to_container(raw), recursive=True)):
            p = Path(match).resolve()
            if p.is_file() and p.is_relative_to(WORKSPACE):
                files[p] = None
    if not files:
        raise ValueError(f"Ни одного файла не найдено: {paths}")
    if len(files) > MAX_FILES:
        raise ValueError(f"Слишком много файлов: {len(files)} > {MAX_FILES}, сузь маску")
    return list(files)


def _host_path(p: Path) -> str:
    return str(HOST_PREFIX.joinpath(*p.relative_to(WORKSPACE).parts))


def _read_files(files: list[Path]) -> tuple[str, int, list[str]]:
    """Склеивает файлы в один блок в пределах лимита. Возвращает текст и заметки о пропусках."""
    parts, notes, budget = [], [], MAX_INPUT_CHARS
    for p in files:
        raw = p.read_bytes()
        if b"\x00" in raw[:8192]:
            notes.append(f"пропущен бинарный: {_host_path(p)}")
            continue
        text = raw.decode("utf-8", errors="replace")
        if budget <= 0:
            notes.append(f"не влез в лимит: {_host_path(p)}")
            continue
        if len(text) > budget:
            # Для логов важнее конец - оставляем хвост.
            text = text[-budget:]
            notes.append(f"обрезан до последних {budget} символов: {_host_path(p)}")
        budget -= len(text)
        parts.append(f'<file path="{_host_path(p)}">\n{text}\n</file>')
    return "\n\n".join(parts), len(parts), notes


@mcp.tool()
async def summarize_files(paths: list[str], question: str) -> str:
    """Командный LLM сам читает файлы с диска пользователя и отвечает на вопрос по ним.

    paths - пути или глобы в C:\\Scimark (например C:\\Scimark\\logs\\**\\*.log).
    Файлы не проходят через контекст вызывающего - возвращается только ответ.
    Используй для больших логов, исходников, конфигов, когда нужна выжимка, а не весь текст.
    """
    files = _resolve_files(paths)
    content, read_count, notes = _read_files(files)
    system = (
        "Ты анализируешь файлы и отвечаешь на вопрос по ним. Отвечай кратко и по делу, "
        "ссылайся на файл и строку/фрагмент, если это важно. Не выдумывай того, чего нет в файлах."
    )
    answer = await _chat([
        {"role": "system", "content": system},
        {"role": "user", "content": f"{content}\n\nВопрос: {question}"},
    ])
    footer = f"\n\n---\nфайлов прочитано: {read_count} из {len(files)}"
    if notes:
        footer += "\n" + "\n".join(notes)
    return answer + footer


@mcp.tool()
async def llm_health() -> dict:
    """Проверить доступность командного LLM: список моделей и их максимальный контекст."""
    async with _client() as client:
        resp = await client.get("/models")
        resp.raise_for_status()
    models = [
        {"id": m.get("id"), "max_model_len": m.get("max_model_len")}
        for m in resp.json().get("data", [])
    ]
    return {
        "base_url": BASE_URL,
        "configured_model": MODEL or None,
        "thinking": THINKING,
        "reasoning_effort": REASONING_EFFORT,
        "models": models,
    }


@mcp.tool()
async def ask(prompt: str, system: str | None = None) -> str:
    """Отдать задачу командному LLM (Qwen) и получить только итоговый ответ.

    Подходит для шаблонного кода, маппингов, DTO, тест-кейсов, выжимок.
    Ответ модели нужно проверять: это 27B-модель, а не замена ревью.
    """
    messages = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": prompt})
    return await _chat(messages)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
