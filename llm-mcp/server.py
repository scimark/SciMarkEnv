"""MCP-сервер: делегирование задач командному LLM через OpenAI-совместимый API."""
import asyncio
import glob
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

import httpx
from mcp.server.fastmcp import FastMCP

import atlassian

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

# Асинхронные задачи: для клиентов с коротким таймаутом на вызов тула.
JOB_TTL = int(os.environ.get("LLM_JOB_TTL", "3600"))          # сек, сколько хранить готовые
MAX_PARALLEL = int(os.environ.get("LLM_MAX_PARALLEL", "16"))  # одновременных запросов к LLM (vLLM батчит)
MAX_WAIT = 40                                                 # сек, с запасом до таймаута клиента (60)

# stateless_http: без серверных сессий - клиенты переживают рестарт контейнера.
mcp = FastMCP("llm", host="0.0.0.0", port=int(os.environ.get("MCP_PORT", "8765")), stateless_http=True)
_llm_slots = asyncio.Semaphore(MAX_PARALLEL)


# ---------- LLM ----------

def _client() -> httpx.AsyncClient:
    if not BASE_URL or not API_KEY:
        raise RuntimeError("LLM_BASE_URL / LLM_API_KEY не заданы в .env")
    return httpx.AsyncClient(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=TIMEOUT,
    )


EFFORTS = ("low", "medium", "xhigh")
CODE_EFFORT = os.environ.get("LLM_CODE_EFFORT", "medium")  # xhigh на коде думает по 10+ минут


async def _chat(messages: list[dict], effort: str | None = None) -> str:
    """Один запрос к модели, стримингом.

    Шлюз llm.predictor.su рвёт непотоковые запросы на ~90 с (504), а xhigh думает дольше.
    Поток держит соединение живым; рассуждения тоже идут в поток, но здесь отбрасываются -
    наружу уходит только итоговый ответ.
    """
    effort = effort or REASONING_EFFORT
    if effort not in EFFORTS:
        raise ValueError(f"effort должен быть одним из {EFFORTS}")
    payload = {
        "model": MODEL or None,
        "messages": messages,
        "thinking": THINKING,
        "reasoning_effort": effort,
        "include_reasoning": True,   # чтобы байты шли и во время рассуждений
        "stream": True,
    }
    parts: list[str] = []
    async with _llm_slots, _client() as client:
        async with client.stream("POST", "/chat/completions", json=payload) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                for choice in json.loads(data).get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        parts.append(piece)
    return "".join(parts).strip()


# ---------- Файлы ----------

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
    """Склеивает файлы в один блок в пределах лимита. Возвращает текст, число файлов и заметки."""
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


# ---------- Сценарии (общие для sync и async тулов) ----------

def _ask_messages(prompt: str, system: str | None) -> list[dict]:
    messages = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": prompt})
    return messages


def _prepare_summarize(paths: list[str], question: str) -> tuple[list[dict], str]:
    """Читает файлы сразу: ошибки путей видны при вызове, а не потом в задаче."""
    files = _resolve_files(paths)
    content, read_count, notes = _read_files(files)
    system = (
        "Ты анализируешь файлы и отвечаешь на вопрос по ним. Отвечай кратко и по делу, "
        "ссылайся на файл и строку/фрагмент, если это важно. Не выдумывай того, чего нет в файлах."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"{content}\n\nВопрос: {question}"},
    ]
    footer = f"\n\n---\nфайлов прочитано: {read_count} из {len(files)}"
    if notes:
        footer += "\n" + "\n".join(notes)
    return messages, footer


# ---------- Задачи ----------

@dataclass
class Job:
    kind: str
    created: float = field(default_factory=time.time)
    finished: float | None = None
    result: str | None = None
    error: str | None = None
    task: asyncio.Task | None = None

    @property
    def status(self) -> str:
        if self.error is not None:
            return "error"
        return "done" if self.finished else "running"

    def view(self, job_id: str) -> dict:
        elapsed = (self.finished or time.time()) - self.created
        out = {"job_id": job_id, "kind": self.kind, "status": self.status, "elapsed_s": int(elapsed)}
        if self.result is not None:
            out["result"] = self.result
        if self.error is not None:
            out["error"] = self.error
        return out


_jobs: dict[str, Job] = {}


def _gc_jobs() -> None:
    now = time.time()
    for job_id in [j for j, job in _jobs.items() if job.finished and now - job.finished > JOB_TTL]:
        del _jobs[job_id]


def _submit(kind: str, messages: list[dict], footer: str = "", effort: str | None = None) -> dict:
    _gc_jobs()
    job_id = uuid.uuid4().hex[:12]
    job = Job(kind=kind)

    async def run() -> None:
        try:
            job.result = await _chat(messages, effort) + footer
        except Exception as e:  # отдаём ошибку клиенту через job_result
            job.error = f"{type(e).__name__}: {e}"
        finally:
            job.finished = time.time()

    job.task = asyncio.create_task(run())
    _jobs[job_id] = job
    return {"job_id": job_id, "status": "running", "hint": "забери ответ через job_result(job_id)"}


# ---------- Тулы ----------

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
        "jobs_running": sum(1 for j in _jobs.values() if j.status == "running"),
    }


@mcp.tool()
async def ask(prompt: str, system: str | None = None, effort: str | None = None) -> str:
    """Отдать задачу командному LLM (Qwen) и дождаться итогового ответа.

    Подходит для шаблонного кода, маппингов, DTO, тест-кейсов, выжимок.
    Ответ модели нужно проверять: это 27B-модель, а не замена ревью.
    Думает долго (до минут). Если у клиента таймаут на тул ~60 с - используй ask_async.
    effort - low | medium | xhigh (по умолчанию xhigh).
    """
    return await _chat(_ask_messages(prompt, system), effort)


@mcp.tool()
async def summarize_files(paths: list[str], question: str) -> str:
    """Командный LLM сам читает файлы с диска пользователя и отвечает на вопрос по ним.

    paths - пути или глобы в C:\\Scimark (например C:\\Scimark\\logs\\**\\*.log).
    Файлы не проходят через контекст вызывающего - возвращается только ответ.
    Думает долго. Если у клиента таймаут на тул ~60 с - используй summarize_files_async.
    """
    messages, footer = _prepare_summarize(paths, question)
    return await _chat(messages) + footer


@mcp.tool()
async def ask_async(prompt: str, system: str | None = None, effort: str | None = None) -> dict:
    """То же, что ask, но сразу возвращает job_id. Ответ забирать через job_result."""
    return _submit("ask", _ask_messages(prompt, system), effort=effort)


@mcp.tool()
async def summarize_files_async(paths: list[str], question: str) -> dict:
    """То же, что summarize_files, но сразу возвращает job_id. Ответ забирать через job_result.

    Ошибки путей (нет файлов, путь вне C:\\Scimark) возвращаются сразу.
    """
    messages, footer = _prepare_summarize(paths, question)
    return _submit("summarize_files", messages, footer)


@mcp.tool()
async def job_result(job_id: str, wait_seconds: int = 35) -> dict:
    """Статус и ответ асинхронной задачи.

    Ждёт готовности до wait_seconds (максимум 40), потом отдаёт текущий статус.
    status: running - вызови ещё раз; done - ответ в result; error - текст в error.
    """
    job = _jobs.get(job_id)
    if job is None:
        return {"job_id": job_id, "status": "not_found", "error": "нет такой задачи или она устарела"}
    if job.task and not job.task.done():
        wait = max(0, min(wait_seconds, MAX_WAIT))
        await asyncio.wait({job.task}, timeout=wait)
    return job.view(job_id)


@mcp.tool()
async def list_jobs() -> list[dict]:
    """Список задач (без текстов ответов): id, тип, статус, сколько секунд идёт."""
    _gc_jobs()
    return [
        {k: v for k, v in job.view(job_id).items() if k not in ("result", "error")}
        for job_id, job in _jobs.items()
    ]


# ---------- Код: квен пишет по ТЗ, вызывающий ревьюит ----------

CODE_SYSTEM = (
    "Ты senior-разработчик. Пиши рабочий, чистый код строго по ТЗ, в стиле переданных файлов проекта. "
    "Ничего не выдумывай за пределами ТЗ; если чего-то не хватает - перечисли в конце в 'Вопросы'.\n"
    "Формат ответа - только файлы и короткие заметки:\n"
    "=== FILE: <путь> ===\n```<язык>\n<полное содержимое файла>\n```\n"
    "...повторить для каждого файла...\n"
    "=== NOTES ===\n<до 5 строк: что сделано, допущения, вопросы>"
)


@mcp.tool()
async def write_code(task: str, context_paths: list[str] | None = None, background: bool = False,
                     effort: str | None = None):
    """Квен пишет код по ТЗ. Сам читает файлы проекта из context_paths (образцы стиля, интерфейсы,
    файл, который надо переделать) - их содержимое не проходит через контекст вызывающего.

    Возвращает файлы целиком в формате '=== FILE: путь ===' + заметки. Код ОБЯЗАТЕЛЬНО ревьюить.
    Годится для шаблонного кода: DTO, мапперы, CRUD, тесты, миграции, рефакторинг по образцу.
    background=true - сразу вернуть job_id, ответ забрать через job_result.
    effort - low | medium | xhigh, по умолчанию medium (xhigh на коде думает по 10+ минут).
    """
    effort = effort or CODE_EFFORT
    content, footer = "", ""
    if context_paths:
        files = _resolve_files(context_paths)
        content, read_count, notes = _read_files(files)
        footer = f"\n\n---\nконтекст: {read_count} из {len(files)} файлов" + "".join(f"\n{n}" for n in notes)
    messages = [
        {"role": "system", "content": CODE_SYSTEM},
        {"role": "user", "content": f"{content}\n\nТЗ:\n{task}" if content else f"ТЗ:\n{task}"},
    ]
    if background:
        return _submit("write_code", messages, footer, effort)
    return await _chat(messages, effort) + footer


# ---------- Jira / Confluence: квен сам читает, наружу только суть ----------

DIGEST_SYSTEM = (
    "Тебе дан материал из Jira/Confluence. Отвечай кратко и по делу, на русском. "
    "Не выдумывай того, чего нет в материале; если чего-то нет - так и скажи. "
    "Ключи задач, имена, версии, даты и цифры переноси точно."
)
DIGEST_DEFAULT_Q = (
    "Дай суть: что нужно сделать или о чём материал, текущее состояние, "
    "ключевые решения и договорённости из комментариев, открытые вопросы."
)


async def _digest(kind: str, material: str, question: str, background: bool, footer: str = ""):
    messages = [
        {"role": "system", "content": DIGEST_SYSTEM},
        {"role": "user", "content": f"{material}\n\nВопрос: {question or DIGEST_DEFAULT_Q}"},
    ]
    if background:
        return _submit(kind, messages, footer)
    return await _chat(messages) + footer


@mcp.tool()
async def jira_digest(issue: str, question: str = "", background: bool = False):
    """Квен сам читает задачу Jira (поля, описание, связи, все комментарии) и отдаёт суть.

    issue - ключ (PROJ-123) или ссылка. question - что именно выяснить; пусто - общая выжимка.
    background=true - сразу вернуть job_id, ответ забрать через job_result.
    Только чтение. Создание и правка задач - не здесь.
    """
    return await _digest("jira_digest", await atlassian.fetch_issue(issue), question, background)


@mcp.tool()
async def jira_search_digest(jql: str, question: str = "", limit: int = 50, background: bool = False):
    """Квен читает задачи по JQL (без комментариев, до 50 штук) и отвечает на вопрос по ним.

    Например: jql='sprint in openSprints() AND assignee = currentUser()', question='что горит?'.
    background=true - сразу вернуть job_id, ответ забрать через job_result.
    """
    text, got, total = await atlassian.search_issues(jql, limit)
    if not got:
        return f"По JQL ничего не найдено: {jql}"
    footer = f"\n\n---\nзадач прочитано: {got} из {total}"
    return await _digest("jira_search_digest", text, question, background, footer)


@mcp.tool()
async def confluence_digest(page: str, question: str = "", with_comments: bool = True,
                            background: bool = False):
    """Квен сам читает страницу Confluence (текст и комментарии) и отдаёт суть.

    page - id, ссылка (?pageId=, /pages/<id>/, /display/SPACE/Title).
    background=true - сразу вернуть job_id, ответ забрать через job_result.
    """
    material = await atlassian.fetch_page(page, with_comments)
    return await _digest("confluence_digest", material, question, background)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
