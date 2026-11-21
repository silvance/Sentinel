"""Try a console program at a real keyboard buffer with nobody at the keyboard (Windows; developer tool).

Why: START-A-NEW-RECORDING-LOG.cmd was tried, before it went to the laptop, with its input fed from a file and its
output captured. So it never met a keyboard buffer: keys that arrive before a question is on the screen, a last
"press any key" that any key ends. On the laptop its first run ended unseen (docs/branches/req-operator-gui.md
section 13.1). A program that a person runs at a keyboard is tried like this instead:

    $p = Start-Process cmd.exe -ArgumentList '/c "script.cmd"' -WindowStyle Hidden -PassThru

gives it a console of its own that never shows on the desktop and cannot take the keys of whoever is using the PC
(a visible console does both). Then, from any other process:

    python tests/tools/console_probe.py read PID          the console's text, and who shares the console
    python tests/tools/console_probe.py key PID "NEW LOG\\r"   key presses into its input, as a keyboard makes them
    python tests/tools/console_probe.py windows           the console windows on the desktop

A console of a program run as administrator cannot be attached to from a process that is not.
"""

from __future__ import annotations

import ctypes
import json
import sys
from ctypes import wintypes

CLASSES = ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "PseudoConsoleWindow")


class COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", wintypes.SHORT), ("Top", wintypes.SHORT), ("Right", wintypes.SHORT),
                ("Bottom", wintypes.SHORT)]


class CSBI(ctypes.Structure):
    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD), ("wAttributes", wintypes.WORD),
                ("srWindow", SMALL_RECT), ("dwMaximumWindowSize", COORD)]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD), ("wVirtualKeyCode", wintypes.WORD),
                ("wVirtualScanCode", wintypes.WORD), ("uChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]


class _Event(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("pad", ctypes.c_byte * 16)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _Event)]


def _libs():  # noqa: ANN202 - (kernel32, user32) with the argument types the calls below need
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u32.IsWindowVisible.argtypes = [wintypes.HWND]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                wintypes.DWORD, wintypes.HANDLE]
    k32.ReadConsoleOutputCharacterW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, COORD,
                                                ctypes.POINTER(wintypes.DWORD)]
    k32.GetConsoleScreenBufferInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(CSBI)]
    k32.WriteConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(INPUT_RECORD), wintypes.DWORD,
                                       ctypes.POINTER(wintypes.DWORD)]
    return k32, u32


def windows() -> list[dict]:
    """The console windows on the desktop (classic, Windows Terminal, and the hidden ones behind a terminal)."""
    _k32, u32 = _libs()
    out: list[dict] = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def each(h, _):  # noqa: ANN001, ANN202
        cls = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(h, cls, 256)
        if cls.value in CLASSES:
            title = ctypes.create_unicode_buffer(512)
            u32.GetWindowTextW(h, title, 512)
            pid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            out.append({"hwnd": int(h or 0), "class": cls.value, "title": title.value, "pid": pid.value,
                        "visible": bool(u32.IsWindowVisible(h))})
        return True

    u32.EnumWindows(proto(each), 0)
    return out


def _attach(k32, pid: int) -> None:  # noqa: ANN001
    k32.FreeConsole()
    if not k32.AttachConsole(pid):
        raise OSError(f"AttachConsole({pid}) failed: error {ctypes.get_last_error()}")


def _open(k32, name: str) -> int:  # noqa: ANN001
    h = k32.CreateFileW(name, 0xC0000000, 3, None, 3, 0, None)          # read and write, shared, must exist
    if h in (None, wintypes.HANDLE(-1).value):
        raise OSError(f"cannot open {name}: error {ctypes.get_last_error()}")
    return h


def read(pid: int) -> dict:
    """The text on the console that process PID is attached to (down to the cursor), and the processes on it."""
    k32, _u32 = _libs()
    _attach(k32, pid)
    procs = (wintypes.DWORD * 32)()
    n = k32.GetConsoleProcessList(procs, 32)
    h = _open(k32, "CONOUT$")
    info = CSBI()
    if not k32.GetConsoleScreenBufferInfo(h, ctypes.byref(info)):
        raise OSError(f"GetConsoleScreenBufferInfo: error {ctypes.get_last_error()}")
    lines = []
    for y in range(info.dwCursorPosition.Y + 1):
        buf = ctypes.create_unicode_buffer(info.dwSize.X + 1)
        got = wintypes.DWORD()
        k32.ReadConsoleOutputCharacterW(h, buf, info.dwSize.X, COORD(0, y), ctypes.byref(got))
        lines.append(buf.value[:got.value].rstrip())
    return {"sharing": sorted(p for p in list(procs)[:n] if p != k32.GetCurrentProcessId()), "text": "\n".join(lines)}


def key(pid: int, text: str) -> int:
    """Put TEXT into that console's input as key presses (``\\r`` is Enter). Returns the records written."""
    k32, u32 = _libs()
    _attach(k32, pid)
    h = _open(k32, "CONIN$")
    sent = 0
    for ch in text.replace("\\r", "\r"):
        vk = 0x0D if ch == "\r" else (u32.VkKeyScanW(ch) & 0xFF)
        for down in (1, 0):
            rec = INPUT_RECORD()
            rec.EventType = 1                                             # KEY_EVENT
            rec.Event.KeyEvent = KEY_EVENT_RECORD(down, 1, vk, 0, ch, 0)
            done = wintypes.DWORD()
            if not k32.WriteConsoleInputW(h, ctypes.byref(rec), 1, ctypes.byref(done)):
                raise OSError(f"WriteConsoleInput: error {ctypes.get_last_error()}")
            sent += done.value
    return sent


def main(argv: list[str]) -> int:
    if sys.platform != "win32" or not argv or argv[0] not in ("windows", "read", "key"):
        print(__doc__)
        return 2
    try:
        if argv[0] == "windows":
            res: object = windows()
        elif argv[0] == "read":
            res = read(int(argv[1]))
        else:
            res = {"records_written": key(int(argv[1]), argv[2])}
    except OSError as exc:
        res = {"error": str(exc)}
    sys.stdout.write(json.dumps(res, indent=1) + "\n")        # a pipe stays usable after the console is let go
    return 0 if not (isinstance(res, dict) and "error" in res) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
