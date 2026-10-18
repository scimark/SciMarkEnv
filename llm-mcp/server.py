"""MCP-сервер: делегирование задач командному LLM через OpenAI-совместимый API."""
import os

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
API_KEY = os.environ.get("LLM_API_KEY", "")
MODEL = os.environ.get("LLM_MODEL", "")
TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "900"))
# Лимитов нет - по умолчанию думаем на максимум.
THINKING = os.environ.get("LLM_THINKING", "true").lower() == "true"
REASONING_EFFORT = os.environ.get("LLM_REASONING_EFFORT", "xhigh")

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
