"""Поиск rudesktop.exe: RUDESKTOP_EXE или автопоиск по типовым местам."""
import os
import re
from pathlib import Path


class ExeNotFound(Exception):
    pass


def _search_roots() -> list[Path]:
    env = os.environ
    candidates = [
        env.get("ProgramFiles"),
        env.get("ProgramFiles(x86)"),
        env.get("LOCALAPPDATA"),
        os.path.join(env.get("LOCALAPPDATA", ""), "Programs"),
        os.path.join(env.get("USERPROFILE", ""), "Desktop"),
        os.path.join(env.get("USERPROFILE", ""), "Downloads"),
    ]
    return [Path(c) for c in candidates if c and Path(c).is_dir()]


def _version_key(path: Path) -> tuple:
    """rudesktop-2.9.927-x32.exe -> (2, 9, 927). Без версии в имени - по дате изменения."""
    m = re.search(r"(\d+(?:\.\d+)+)", path.name)
    version = tuple(int(x) for x in m.group(1).split(".")) if m else ()
    return version, path.stat().st_mtime


def find_rudesktop_exe() -> str:
    explicit = os.environ.get("RUDESKTOP_EXE")
    if explicit:
        if Path(explicit).is_file():
            return explicit
        raise ExeNotFound(f"RUDESKTOP_EXE указывает на несуществующий файл: {explicit}")

    found: list[Path] = []
    for root in _search_roots():
        # корень + один уровень вложенности (Program Files\RuDesktop\rudesktop.exe)
        found += [p for p in root.glob("rudesktop*.exe") if p.is_file()]
        found += [p for p in root.glob("*/rudesktop*.exe") if p.is_file()]
    if not found:
        raise ExeNotFound(
            "rudesktop*.exe не найден в Program Files, LocalAppData, Desktop, Downloads. "
            "Укажи путь в переменной RUDESKTOP_EXE"
        )
    return str(max(found, key=_version_key))
