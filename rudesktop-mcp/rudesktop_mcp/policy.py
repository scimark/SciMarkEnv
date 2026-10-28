"""Режимы выполнения команд.

confirm - каждая команда через окошко подтверждения
smart   - read-only команды сами, остальное через окошко
auto    - всё без вопросов

Классификатор в smart намеренно параноидальный: если не уверен, что команда
только читает, считаем её записью и спрашиваем.
"""
import re
from enum import Enum


class Mode(str, Enum):
    CONFIRM = "confirm"
    SMART = "smart"
    AUTO = "auto"

    @property
    def rank(self) -> int:
        return {"confirm": 0, "smart": 1, "auto": 2}[self.value]


READONLY_VERBS = {
    "get", "test", "select", "where", "sort", "measure", "format", "resolve", "find",
    "compare", "group", "convertto", "convertfrom", "join", "split", "search", "show",
}
READONLY_CMDLETS = {
    "out-string", "out-host", "write-output", "write-host", "set-location", "push-location",
    "pop-location",
}
READONLY_NATIVE = {
    # алиасы PowerShell
    "dir", "ls", "gci", "gc", "cat", "type", "echo", "pwd", "gl", "gps", "ps", "gsv", "gwmi",
    "gcim", "sls", "select", "where", "?", "sort", "measure", "fl", "ft", "fw", "cd", "chdir",
    "sl", "pushd", "popd", "history", "h", "gm", "gi", "gp", "gu", "gv", "gal", "gcm",
    # нативные утилиты, которые только читают
    "hostname", "whoami", "ipconfig", "ping", "tracert", "nslookup", "netstat", "tasklist",
    "systeminfo", "findstr", "more", "tree", "where.exe", "ver", "query", "quser", "arp",
    "route", "getmac", "driverquery", "wmic",
}
# опасное в любом месте команды
_DANGEROUS = [
    re.compile(r"(?<![\w-])(iex|invoke-expression|invoke-command|icm|start-process|saps|start)\b", re.I),
    re.compile(r"(?<![<-])>{1,2}(?!&|\s*\$null)"),  # редирект в файл (2>$null и 2>&1 можно)
    re.compile(r"\)\s*\.\s*\w+\s*\(|\]::\w+\s*\("),   # вызовы методов: (x).Delete(), [IO.File]::Delete()
    re.compile(r"-(command|encodedcommand|enc|c)\b", re.I),
    re.compile(r"\bwmic\b.*\b(call|delete|create|set)\b", re.I),
    re.compile(r"\broute\b\s+(add|delete|change)\b", re.I),
    re.compile(r"\barp\b\s+-[ds]\b", re.I),
]
_VERB_NOUN = re.compile(r"(?<![\w$.-])([A-Za-z]+)-([A-Za-z]+)\b")
_SEGMENT_SPLIT = re.compile(r"\|\||&&|[;|{}()\n]")
_ASSIGN = re.compile(r"^\$[\w:]+\s*[+\-*/]?=\s*")


def _strip_strings(command: str) -> str:
    """Выкидываем содержимое строк, чтобы 'Remove-Item' в тексте сообщения не мешал и не помогал."""
    return re.sub(r"'[^']*'|\"[^\"]*\"", "''", command)


def is_read_only(command: str) -> bool:
    raw = command
    code = _strip_strings(command)
    # $(...) внутри двойных кавычек выполняется, такие строки не прощаем
    if re.search(r"\"[^\"]*\$\(", raw):
        return False
    if any(p.search(code) for p in _DANGEROUS):
        return False

    # все Verb-Noun в команде должны быть читающими
    for verb, noun in _VERB_NOUN.findall(code):
        name = f"{verb}-{noun}".lower()
        if verb.lower() not in READONLY_VERBS and name not in READONLY_CMDLETS:
            return False

    # первая лексема каждого сегмента: cmdlet, алиас или нативная утилита из белого списка
    for segment in _SEGMENT_SPLIT.split(code):
        seg = _ASSIGN.sub("", segment.strip())
        seg = seg.lstrip("&. ").strip()
        if not seg:
            continue
        first = seg.split()[0].lower()
        if first.startswith("$") or first in ("''", "$true", "$false", "$null"):
            continue
        if re.fullmatch(r"-?\d+(\.\d+)?", first) or first.startswith("-") or first.startswith("["):
            continue  # литералы, параметры после { }, приведения типов
        if first in READONLY_NATIVE or first in READONLY_CMDLETS:
            continue
        if _VERB_NOUN.fullmatch(first) and first.split("-")[0] in READONLY_VERBS:
            continue
        if first in ("foreach-object", "%", "foreach", "if", "else", "elseif", "in"):
            continue  # тело блока проверяется отдельными сегментами
        return False
    return True


def needs_confirmation(mode: Mode, command: str) -> bool:
    if mode is Mode.AUTO:
        return False
    if mode is Mode.SMART:
        return not is_read_only(command)
    return True
