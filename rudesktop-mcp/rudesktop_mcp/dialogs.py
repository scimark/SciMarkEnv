"""Локальные окошки на рабочем столе: ID с паролем и подтверждения.

Всё, что решает человек, спрашивается здесь, а не через MCP. Так модель не может
сама себе ответить «да», а пароль не попадает в ответы тулз и логи.
"""
import ctypes
import tkinter as tk
from typing import Optional

_VK_MENU = 0x12
_KEYEVENTF_KEYUP = 0x2
_VK_A, _VK_V, _VK_C = 0x41, 0x56, 0x43


def _grab_focus(root: tk.Tk, widget: tk.Widget) -> None:
    """Windows не даёт фоновому процессу забрать фокус клавиатуры.
    Обходим через фейковое нажатие Alt + SetForegroundWindow."""
    user32 = ctypes.windll.user32
    hwnd = int(root.wm_frame(), 16)
    user32.keybd_event(_VK_MENU, 0, 0, 0)
    user32.SetForegroundWindow(hwnd)
    user32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)
    root.focus_force()
    widget.focus_set()


def _ctrl_shortcuts(event: tk.Event) -> Optional[str]:
    """Tk биндит Ctrl+V по keysym, а на русской раскладке keysym кириллический,
    поэтому смотрим на физическую клавишу (keycode). Ctrl+A в Tk по умолчанию
    прыгает в начало строки, переделываем на выделить всё."""
    widget = event.widget
    if event.keycode == _VK_V:
        widget.event_generate("<<Paste>>")
        return "break"
    if event.keycode == _VK_C and isinstance(widget, tk.Text):
        widget.event_generate("<<Copy>>")
        return "break"
    if event.keycode == _VK_A:
        if isinstance(widget, tk.Text):
            widget.tag_add("sel", "1.0", "end")
        else:
            widget.select_range(0, "end")
            widget.icursor("end")
        return "break"
    return None


def _make_root(title: str) -> tk.Tk:
    root = tk.Tk()
    root.title(title)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    return root


def _show(root: tk.Tk, focus: tk.Widget) -> None:
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 3
    root.geometry(f"+{x}+{y}")
    root.after(100, lambda: _grab_focus(root, focus))
    root.mainloop()


def normalize_id(target_id: str) -> str:
    """ID RuDesktop без пробелов: "234 968 320" -> "234968320"."""
    return "".join(target_id.split())


def ask_credentials(target_id: Optional[str] = None) -> Optional[tuple[str, str]]:
    """ID и пароль сервера. Если target_id передан, поле ID уже заполнено. None, если отменили."""
    result: dict = {"value": None}
    root = _make_root("RuDesktop: подключение")

    form = tk.Frame(root)
    form.pack(padx=16, pady=(14, 4))
    tk.Label(form, text="ID RuDesktop:").grid(row=0, column=0, sticky="w", pady=4)
    id_entry = tk.Entry(form, width=28)
    id_entry.grid(row=0, column=1, padx=(8, 0), pady=4)
    tk.Label(form, text="Пароль:").grid(row=1, column=0, sticky="w", pady=4)
    pwd_entry = tk.Entry(form, show="*", width=28)
    pwd_entry.grid(row=1, column=1, padx=(8, 0), pady=4)

    for entry in (id_entry, pwd_entry):
        entry.bind("<Control-KeyPress>", _ctrl_shortcuts)
    if target_id:
        id_entry.insert(0, target_id)

    error = tk.Label(root, text="", fg="red")
    error.pack(padx=16, anchor="w")

    def ok(_event=None):
        tid = normalize_id(id_entry.get())
        pwd = pwd_entry.get()
        if not tid:
            error.configure(text="Введи ID")
            id_entry.focus_set()
            return
        if not pwd:
            error.configure(text="Введи пароль")
            pwd_entry.focus_set()
            return
        result["value"] = (tid, pwd)
        root.destroy()

    def cancel(_event=None):
        root.destroy()

    def next_field(_event=None):
        pwd_entry.focus_set()
        return "break"

    buttons = tk.Frame(root)
    buttons.pack(padx=16, pady=(4, 14), anchor="e")
    tk.Button(buttons, text="Подключиться", command=ok).pack(side="left", padx=(0, 6))
    tk.Button(buttons, text="Отмена", command=cancel).pack(side="left")
    id_entry.bind("<Return>", next_field)
    root.bind("<Return>", ok)
    root.bind("<Escape>", cancel)
    root.protocol("WM_DELETE_WINDOW", cancel)

    _show(root, pwd_entry if target_id else id_entry)
    return result["value"]


def ask_confirm(title: str, header: str, body: str, ok_label: str = "Выполнить") -> bool:
    """Подтверждение с текстом (команда, режим). Enter не подтверждает, чтобы не жмакнуть случайно."""
    result = {"value": False}
    root = _make_root(title)

    tk.Label(root, text=header, justify="left", font=("Segoe UI", 10, "bold")).pack(
        padx=16, pady=(14, 6), anchor="w"
    )
    lines = min(max(body.count("\n") + 1, 3), 20)
    text = tk.Text(root, width=90, height=lines, wrap="word", font=("Consolas", 10))
    text.insert("1.0", body)
    text.configure(state="disabled")
    text.bind("<Control-KeyPress>", _ctrl_shortcuts)
    text.pack(padx=16, pady=4)

    def ok():
        result["value"] = True
        root.destroy()

    def cancel(_event=None):
        root.destroy()

    buttons = tk.Frame(root)
    buttons.pack(padx=16, pady=(6, 14), anchor="e")
    deny = tk.Button(buttons, text="Отклонить", command=cancel)
    tk.Button(buttons, text=ok_label, command=ok).pack(side="left", padx=(0, 6))
    deny.pack(side="left")
    root.bind("<Escape>", cancel)
    root.protocol("WM_DELETE_WINDOW", cancel)

    _show(root, deny)
    return result["value"]
