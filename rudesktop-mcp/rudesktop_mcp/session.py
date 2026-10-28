"""Сессия RuDesktop --shell под WinPTY: текстовый ввод-вывод без окон."""
import base64
import os
import re
import threading
import time
import uuid
from typing import Callable, Optional

from winpty import Backend, PtyProcess

from .exe_locator import find_rudesktop_exe

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[=>()][0-9A-B]?")

PASSWORD_PROMPT = re.compile(r"Enter your password:\s*$")
WRONG_PASSWORD = re.compile(r"Wrong Password", re.IGNORECASE)
SHELL_PROMPT = re.compile(r"(?:PS [^\r\n]*>|[A-Za-z]:\\[^\r\n]*>|[\w.-]+@[\w.-]+[^\r\n]*[$#])\s*$")


class SessionError(Exception):
    pass


class RuDesktopSession:
    def __init__(self, target_id: str, exe: Optional[str] = None):
        self.target_id = target_id
        self.session_id = uuid.uuid4().hex[:8]
        self.exe = exe or find_rudesktop_exe()
        self._buf = ""
        self._cond = threading.Condition()
        self._eof = False
        self._proc: Optional[PtyProcess] = None
        self._pos = 0
        self._pending: Optional[dict] = None
        self._run_lock = threading.Lock()

    # --- низкий уровень ---

    def _reader(self) -> None:
        while True:
            try:
                chunk = self._proc.read(65536)
            except EOFError:
                with self._cond:
                    self._eof = True
                    self._cond.notify_all()
                return
            with self._cond:
                self._buf += ANSI.sub("", chunk).replace("\r\n", "\n")
                self._cond.notify_all()

    def _wait_for(self, patterns: dict[str, re.Pattern], start: int, timeout: float) -> tuple[str, int]:
        """Ждёт первый паттерн в буфере начиная с start. Возвращает (имя, конец совпадения)."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                tail = self._buf[start:]
                for name, pattern in patterns.items():
                    m = pattern.search(tail)
                    if m:
                        return name, start + m.end()
                if self._eof:
                    raise SessionError(
                        "Сессия RuDesktop отвалилась (обрыв, лимит времени или сервер закрыл). "
                        f"Переподключись через connect. Последний вывод: {tail.strip()[-500:]}"
                    )
                left = deadline - time.monotonic()
                if left <= 0:
                    raise SessionError(f"Таймаут ожидания. Последний вывод: {tail.strip()[-500:]}")
                self._cond.wait(left)

    def _write(self, text: str) -> None:
        self._proc.write(text)

    # --- API ---

    def connect(self, ask_password: Callable[[str], Optional[str]], timeout: float = 30) -> str:
        """Запускает --shell, спрашивает пароль локально, ждёт промпт шелла. Возвращает приветствие."""
        # WinPTY держит настоящую скрытую консоль (conhost), с ней RuDesktop работает.
        # Под ConPTY коннект к удалённой консоли отваливается.
        backend = Backend.ConPTY if os.environ.get("RUDESKTOP_PTY") == "conpty" else Backend.WinPTY
        self._proc = PtyProcess.spawn(
            [self.exe, "--shell", self.target_id], dimensions=(50, 400), backend=backend
        )
        threading.Thread(target=self._reader, daemon=True).start()
        stage = "ожидание запроса пароля"
        try:
            _, pos = self._wait_for({"password": PASSWORD_PROMPT}, 0, timeout)
            stage = "ввод пароля"
            password = ask_password(self.target_id)
            if password is None:
                raise SessionError("Ввод пароля отменён")
            if not password:
                raise SessionError("Пустой пароль, не отправляю")
            self._write(password + "\r\n")
            del password
            stage = "открытие удалённой консоли (пароль отправлен)"
            name, end = self._wait_for({"wrong": WRONG_PASSWORD, "shell": SHELL_PROMPT}, pos, timeout)
            if name == "wrong":
                raise SessionError("Неверный пароль. Повторять не буду, чтобы не словить бан")
            self._pos = end
            return self._buf[pos:end].strip()
        except SessionError as e:
            self.close()
            raise SessionError(f"[{stage}] {e}") from e
        except Exception:
            self.close()
            raise

    def run(self, command: str, timeout: float = 60) -> dict:
        """Выполняет команду в удалённом PowerShell.

        Экран, который отдаёт WinPTY, полон мусора от перерисовки (пустые строки, \\r,
        обрывки эха). Поэтому вывод команды не парсим с экрана: на удалённой стороне
        он собирается в строку, кодируется в base64 и печатается между маркерами.
        Маркеры склеиваются конкатенацией ("__B"+"64_..."), поэтому в эхе команды
        их нет. Из base64 выкидываем всё, что не base64, и мусор экрана не страшен.
        Команда выполняется через dot-source, так что переменные и cd живут между run().
        """
        if not self.alive:
            raise SessionError("Сессия закрыта")
        if "\n" in command or "\r" in command:
            raise SessionError("Многострочные команды не поддерживаются, склей через ;")
        with self._run_lock:
            if self._pending:
                raise SessionError("Предыдущая команда ещё не завершилась, сначала read()")
            uid = uuid.uuid4().hex[:8]
            wrapped = (
                "$global:LASTEXITCODE=0; $global:__ok=$false; "
                f"$__o = . {{ {command}; $global:__ok=$? }} *>&1 | Out-String -Width 400; "
                f'Write-Output ("__B"+"64_{uid}:"'
                " + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($__o))"
                f' + ":" + $global:__ok + ":" + $global:LASTEXITCODE + ":E"+"ND_{uid}")'
            )
            self._pending = {"uid": uid, "start": self._pos}
            self._write(wrapped + "\r\n")
            return self._collect(timeout)

    def read(self, timeout: float = 30) -> dict:
        """Дочитывает вывод команды, которая не успела завершиться в run()."""
        with self._run_lock:
            if not self._pending:
                return {"output": "", "done": True, "exit_code": None, "success": None}
            return self._collect(timeout)

    def _collect(self, timeout: float) -> dict:
        p = self._pending
        uid = p["uid"]
        done_re = re.compile(
            rf"__B64_{uid}:([A-Za-z0-9+/=\s]*?):\s*(True|False)\s*:\s*(-?\d*)\s*:\s*END_{uid}"
        )
        try:
            _, marker_end = self._wait_for({"done": done_re}, p["start"], timeout)
        except SessionError:
            if self._eof:
                self._pending = None
                raise
            return {"output": "", "done": False, "exit_code": None, "success": None,
                    "note": "Команда ещё выполняется. Вывод придёт целиком по завершении, дочитай через read()"}

        with self._cond:
            m = done_re.search(self._buf[p["start"]:marker_end])
        b64 = re.sub(r"[^A-Za-z0-9+/=]", "", m.group(1))
        output = base64.b64decode(b64).decode("utf-8", errors="replace").replace("\r\n", "\n").strip("\n").rstrip()
        # ждём промпт после маркера, чтобы следующая команда стартовала с чистого места
        try:
            _, self._pos = self._wait_for({"shell": SHELL_PROMPT}, marker_end, 10)
        except SessionError:
            self._pos = marker_end
        self._pending = None
        ok = m.group(2) == "True"
        code = int(m.group(3)) if m.group(3) else (0 if ok else 1)
        return {"output": output, "done": True, "exit_code": code, "success": ok}

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.isalive():
                self._write("exit\r\n")
                time.sleep(1)
        except Exception:
            pass
        finally:
            if self._proc.isalive():
                self._proc.terminate(force=True)
            self._proc = None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.isalive()
