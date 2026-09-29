"""Input injection via SendInput, targeted at the attached desktop.

SendInput events posted by a thread attached to a desktop land on *that*
desktop's windows — the user's foreground desktop is never touched.

Coordinates are desktop physical pixels (same space as GDI capture on the
same thread). MOUSEEVENTF_ABSOLUTE_V2 normalizes over the *virtual screen*
(multi-monitor safe) with 0..65535 per axis.
"""

import time
from ctypes import byref, windll, wintypes

from .defs import (
    INPUT,
    INPUT_KEYBOARD,
    INPUT_MOUSE,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    MOUSEEVENTF_ABSOLUTE_V2,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MIDDLEUP,
    MOUSEEVENTF_MIDDLEDOWN,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    MOUSEEVENTF_WHEEL,
    POINT,
    VK_TABLE,
    WHEEL_DELTA,
)
from . import dpi as _dpi

user32 = windll.user32

EXTENDED_VK = {
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,  # pgup/pgdn/end/home/arrows
    0x2D, 0x2E, 0x2C, 0x153, 0x154, 0x155, 0x156,
}

_BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def _send(inputs: list[INPUT]) -> int:
    user32.SendInput.argtypes = [wintypes.UINT, wintypes.LPVOID, c_int_sizeof()]
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, byref(arr), ctypes_sizeof_input())
    if sent != n:
        raise RuntimeError(f"SendInput returned {sent} of {n} (winerror={windll.kernel32.GetLastError()})")
    return sent


def c_int_sizeof():
    from ctypes import c_int

    return c_int


def ctypes_sizeof_input():
    from ctypes import sizeof

    return sizeof(INPUT)


def _abs_norm(x: int, y: int):
    vxs, vys, vsw, svh = _dpi.virtual_screen_rect()
    nx = int(round((x - vxs) * 65535.0 / vsw))
    ny = int(round((y - vys) * 65535.0 / svh))
    nx = max(0, min(65535, nx))
    ny = max(0, min(65535, ny))
    return nx, ny


def _mouse_event(flags, nx, ny, data=0) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.u.mi.dx = nx
    inp.u.mi.dy = ny
    inp.u.mi.mouseData = data
    inp.u.mi.dwFlags = flags
    inp.u.mi.dwExtraInfo = 0
    return inp


def move_to(x: int, y: int) -> dict:
    nx, ny = _abs_norm(x, y)
    _send([_mouse_event(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE_V2, nx, ny)])
    p = POINT()
    user32.GetCursorPos(byref(p))
    return {"ok": True, "cursorPos": [p.x, p.y]}


def click(x: int, y: int, button: str = "left", double: bool = False, duration_ms: int = 0) -> dict:
    down, up = _BUTTON_FLAGS[button]
    events = [_mouse_event(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE_V2, *_abs_norm(x, y))]
    for i in range(2 if double else 1):
        events.append(_mouse_event(down | MOUSEEVENTF_ABSOLUTE_V2, *_abs_norm(x, y)))
        if duration_ms:
            time.sleep(duration_ms / 1000.0)
        events.append(_mouse_event(up | MOUSEEVENTF_ABSOLUTE_V2, *_abs_norm(x, y)))
        if i < 1:
            time.sleep(0.03)
    _send(events)
    p = POINT()
    user32.GetCursorPos(byref(p))
    return {"ok": True, "applied": [{"type": "click", "x": x, "y": y, "button": button}], "cursorPos": [p.x, p.y]}


def drag(x1: int, y1: int, x2: int, y2: int, steps: int = 12) -> dict:
    if steps < 2:
        steps = 2
    events = [
        _mouse_event(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE_V2, *_abs_norm(x1, y1)),
        _mouse_event(_BUTTON_FLAGS["left"][0] | MOUSEEVENTF_ABSOLUTE_V2, *_abs_norm(x1, y1)),
    ]
    for i in range(1, steps + 1):
        t = i / steps
        cx = int(round(x1 + (x2 - x1) * t))
        cy = int(round(y1 + (y2 - y1) * t))
        events.append(_mouse_event(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE_V2, *_abs_norm(cx, cy)))
        time.sleep(0.008)  # let the target app register the drag
    events.append(_mouse_event(_BUTTON_FLAGS["left"][1] | MOUSEEVENTF_ABSOLUTE_V2, *_abs_norm(x2, y2)))
    _send(events)
    p = POINT()
    user32.GetCursorPos(byref(p))
    return {"ok": True, "applied": [{"type": "drag", "from": [x1, y1], "to": [x2, y2]}], "cursorPos": [p.x, p.y]}


def scroll(x: int, y: int, delta: int) -> dict:
    """delta: positive = scroll up (content moves down)."""
    nx, ny = _abs_norm(x, y)
    _send([
        _mouse_event(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE_V2, nx, ny),
        _mouse_event(MOUSEEVENTF_WHEEL | MOUSEEVENTF_ABSOLUTE_V2, nx, ny, data=delta * WHEEL_DELTA),
    ])
    return {"ok": True, "applied": [{"type": "scroll", "x": x, "y": y, "delta": delta}]}


def _key_event(flags, vk=0, scan=0) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki.wVk = vk
    inp.u.ki.wScan = scan
    inp.u.ki.dwFlags = flags
    inp.u.ki.dwExtraInfo = 0
    return inp


def type_text(text: str, per_char_delay_ms: int = 0) -> dict:
    """Unicode input: bypasses IME entirely, Chinese/emoji code units work."""
    code_units = text.encode("utf-16-le")
    code_units = [code_units[i] | (code_units[i + 1] << 8) for i in range(0, len(code_units), 2)]
    events: list[INPUT] = []
    for cu in code_units:
        events.append(_key_event(KEYEVENTF_UNICODE, scan=cu))
        events.append(_key_event(KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, scan=cu))
        if per_char_delay_ms and len(events) // 2 < len(code_units):
            time.sleep(per_char_delay_ms / 1000.0)
    _send(events)
    return {"ok": True, "sent": len(code_units), "method": "unicode"}


def _vk(name: str) -> int:
    key = name.strip().lower()
    vk = VK_TABLE.get(key)
    if vk is None and len(key) == 1:
        vk = ord(key.upper())
    if vk is None:
        raise ValueError(f"unknown key name: {name!r}")
    return vk


def press_key(name: str, repeat: int = 1) -> dict:
    vk = _vk(name)
    ext = KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_VK else 0
    events: list[INPUT] = []
    for _ in range(max(1, repeat)):
        events.append(_key_event(ext, vk=vk))
        events.append(_key_event(ext | KEYEVENTF_KEYUP, vk=vk))
    _send(events)
    return {"ok": True, "sent": repeat, "method": "vk"}


def hotkey(keys, hold_ms: int = 0) -> dict:
    """keys: list like ['ctrl', 'shift', 's'] — all but the last are modifiers."""
    if not keys:
        raise ValueError("hotkey needs at least one key")
    vk_list = [_vk(k) for k in keys]
    events: list[INPUT] = []
    for vk in vk_list[:-1]:
        ext = KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_VK else 0
        events.append(_key_event(ext, vk=vk))
    last = vk_list[-1]
    ext = KEYEVENTF_EXTENDEDKEY if last in EXTENDED_VK else 0
    events.append(_key_event(ext, vk=last))
    if hold_ms:
        time.sleep(hold_ms / 1000.0)
    events.append(_key_event(ext | KEYEVENTF_KEYUP, vk=last))
    for vk in reversed(vk_list[:-1]):
        ext2 = KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_VK else 0
        events.append(_key_event(ext2 | KEYEVENTF_KEYUP, vk=vk))
    _send(events)
    return {"ok": True, "sent": len(keys), "method": "vk", "keys": list(keys)}