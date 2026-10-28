"""Аудит: кто, куда, что выполнил и с каким решением. Вывод команд не пишем."""
import json
import os
from datetime import datetime
from pathlib import Path

LOG_PATH = Path(
    os.environ.get("RUDESKTOP_AUDIT_LOG")
    or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "rudesktop-mcp" / "audit.jsonl"
)


def log(event: str, **fields) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **fields}
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # аудит не должен ронять работу
