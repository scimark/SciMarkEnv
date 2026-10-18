"""Только чтение Jira/Confluence (Server/DC, PAT). Отдаёт компактный текст для LLM."""
import os
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote_plus, urlparse

import httpx

JIRA_URL = os.environ.get("JIRA_URL", "").rstrip("/")
JIRA_TOKEN = os.environ.get("JIRA_PERSONAL_TOKEN", "")
CONFLUENCE_URL = os.environ.get("CONFLUENCE_URL", "").rstrip("/")
CONFLUENCE_TOKEN = os.environ.get("CONFLUENCE_PERSONAL_TOKEN", "")
SSL_VERIFY = os.environ.get("ATLASSIAN_SSL_VERIFY", "false").lower() == "true"
MAX_SEARCH = 50

ISSUE_KEY = re.compile(r"[A-Z][A-Z0-9]+-\d+")


def _client(base: str, token: str, name: str) -> httpx.AsyncClient:
    if not base or not token:
        raise RuntimeError(f"{name}: URL / PERSONAL_TOKEN не заданы в .env")
    return httpx.AsyncClient(
        base_url=base,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        verify=SSL_VERIFY,
        timeout=60,
    )


def _name(user: dict | None) -> str:
    return (user or {}).get("displayName") or "-"


# ---------- Jira ----------

ISSUE_FIELDS = ",".join([
    "summary", "issuetype", "status", "priority", "assignee", "reporter", "created", "updated",
    "labels", "components", "fixVersions", "description", "comment", "issuelinks", "subtasks", "parent",
])


def issue_key(issue: str) -> str:
    """PROJ-123 или ссылка на задачу -> PROJ-123."""
    m = ISSUE_KEY.search(issue.upper())
    if not m:
        raise ValueError(f"Не похоже на ключ задачи Jira: {issue}")
    return m.group(0)


def _format_issue(data: dict, with_comments: bool = True) -> str:
    f = data["fields"]
    lines = [
        f"# {data['key']}: {f.get('summary')}",
        f"Тип: {(f.get('issuetype') or {}).get('name')} | Статус: {(f.get('status') or {}).get('name')}"
        f" | Приоритет: {(f.get('priority') or {}).get('name')}",
        f"Исполнитель: {_name(f.get('assignee'))} | Автор: {_name(f.get('reporter'))}"
        f" | Создана: {f.get('created', '')[:10]} | Обновлена: {f.get('updated', '')[:10]}",
    ]
    if f.get("parent"):
        lines.append(f"Родитель: {f['parent']['key']} {f['parent']['fields'].get('summary')}")
    meta = {
        "Метки": f.get("labels") or [],
        "Компоненты": [c["name"] for c in f.get("components") or []],
        "Версии": [v["name"] for v in f.get("fixVersions") or []],
    }
    lines += [f"{k}: {', '.join(v)}" for k, v in meta.items() if v]
    links = []
    for link in f.get("issuelinks") or []:
        other = link.get("outwardIssue") or link.get("inwardIssue")
        rel = link["type"]["outward" if "outwardIssue" in link else "inward"]
        links.append(f"- {rel} {other['key']} {other['fields'].get('summary')}")
    links += [f"- подзадача {s['key']} {s['fields'].get('summary')}" for s in f.get("subtasks") or []]
    if links:
        lines += ["Связи:"] + links
    lines += ["", "## Описание", f.get("description") or "(пусто)"]
    if with_comments:
        comments = (f.get("comment") or {}).get("comments") or []
        if comments:
            lines += ["", f"## Комментарии ({len(comments)})"]
            for c in comments:
                lines.append(f"[{c.get('created', '')[:16]}] {_name(c.get('author'))}:\n{c.get('body', '')}")
    return "\n".join(lines)


async def fetch_issue(issue: str) -> str:
    key = issue_key(issue)
    async with _client(JIRA_URL, JIRA_TOKEN, "Jira") as client:
        resp = await client.get(f"/rest/api/2/issue/{key}", params={"fields": ISSUE_FIELDS})
        resp.raise_for_status()
    return _format_issue(resp.json())


async def search_issues(jql: str, limit: int = MAX_SEARCH) -> tuple[str, int, int]:
    """Возвращает текст, сколько задач отдали и сколько всего подходит под JQL."""
    async with _client(JIRA_URL, JIRA_TOKEN, "Jira") as client:
        resp = await client.get("/rest/api/2/search", params={
            "jql": jql, "maxResults": min(limit, MAX_SEARCH),
            "fields": ISSUE_FIELDS.replace(",comment", ""),
        })
        resp.raise_for_status()
    data = resp.json()
    issues = data.get("issues") or []
    text = "\n\n".join(_format_issue(i, with_comments=False) for i in issues)
    return text, len(issues), data.get("total", len(issues))


# ---------- Confluence ----------

class _Text(HTMLParser):
    """Storage-формат Confluence (XHTML) -> плоский текст, таблицы через ' | '."""
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote"}

    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag in ("h1", "h2", "h3", "h4"):
            self.out.append("#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("- ")
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_data(self, data):
        self.out.append(data)

    def text(self) -> str:
        raw = "".join(self.out)
        return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", raw)).strip()


def html_to_text(html: str) -> str:
    parser = _Text()
    parser.feed(html or "")
    return parser.text()


async def _resolve_page_id(client: httpx.AsyncClient, page: str) -> str:
    """id, ссылка с pageId=, /pages/<id>/ или /display/SPACE/Title -> id страницы."""
    if page.isdigit():
        return page
    url = urlparse(page)
    if "pageId" in parse_qs(url.query):
        return parse_qs(url.query)["pageId"][0]
    m = re.search(r"/pages/(\d+)", url.path)
    if m:
        return m.group(1)
    m = re.search(r"/display/([^/]+)/([^/?#]+)", url.path)
    if m:
        resp = await client.get("/rest/api/content", params={
            "spaceKey": m.group(1), "title": unquote_plus(m.group(2)), "type": "page",
        })
        resp.raise_for_status()
        results = resp.json().get("results") or []
        if results:
            return results[0]["id"]
    raise ValueError(f"Не удалось понять страницу Confluence: {page}")


async def fetch_page(page: str, with_comments: bool = True) -> str:
    async with _client(CONFLUENCE_URL, CONFLUENCE_TOKEN, "Confluence") as client:
        page_id = await _resolve_page_id(client, page)
        resp = await client.get(f"/rest/api/content/{page_id}", params={
            "expand": "body.storage,version,space,ancestors",
        })
        resp.raise_for_status()
        data = resp.json()
        comments = []
        if with_comments:
            cr = await client.get(f"/rest/api/content/{page_id}/child/comment", params={
                "expand": "body.storage,history", "limit": 100,
            })
            cr.raise_for_status()
            comments = cr.json().get("results") or []
    path = " / ".join(a["title"] for a in data.get("ancestors") or [])
    lines = [
        f"# {data['title']} (id {page_id})",
        f"Пространство: {data['space']['key']} | Путь: {path or '-'}"
        f" | Версия: {data['version']['number']} от {data['version'].get('when', '')[:10]}"
        f" ({_name(data['version'].get('by'))})",
        "",
        html_to_text(data["body"]["storage"]["value"]),
    ]
    if comments:
        lines += ["", f"## Комментарии ({len(comments)})"]
        for c in comments:
            author = _name((c.get("history") or {}).get("createdBy"))
            lines.append(f"{author}: {html_to_text(c['body']['storage']['value'])}")
    return "\n".join(lines)
