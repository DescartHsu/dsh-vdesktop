"""D2 输入降级链：SendInput → PostMessage → SendMessage → 目标桌面 WH 钩子。

环境变量 INPUT_FALLBACK=auto|sendinput|postmessage|sendmessage|hook。
- auto：全链
- 其他值：只走那一级（调试用）
"""
from __future__ import annotations

import ctypes
import os
import re
import time
from ctypes import byref, c_void_p, WINFUNCTYPE
from ctypes.wintypes import HWND, DWORD, BOOL, UINT, LPARAM, WPARAM, RECT, POINT

from dshv.win32 import input as win_input

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_CHAR = 0x0102
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002

VK_C = 0x43
VK_D = 0x44
VK_E = 0x45

# M5a: sidecar 自己维护一份最后已知光标坐标（vdesk 非前台，SetCursorPos 无效）。
_LAST_CURSOR = {"x": 0, "y": 0, "visible": True}

def _set_last_cursor(x, y):
    _LAST_CURSOR["x"] = int(x)
    _LAST_CURSOR["y"] = int(y)
    _LAST_CURSOR["visible"] = True

def last_cursor():
    return dict(_LAST_CURSOR)


_PM_RE = re.compile(r"returned (\d+) of (\d+)")
_ERR_RE = re.compile(r"winerror=(\d+)")

def _set_proto():
    user32.GetWindowTextLengthW.argtypes = [HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [HWND, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [HWND, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [HWND]
    user32.IsWindowVisible.restype = BOOL
    # NOTE: no argtypes for GetWindowRect here on purpose — process.py passes
    # byref(defs.RECT) while this module uses wintypes.RECT; a typed argtype
    # with one RECT class breaks the other ("expected LP_RECT instance ...").
    user32.GetClientRect.argtypes = [HWND, ctypes.POINTER(RECT)]
    user32.ScreenToClient.argtypes = [HWND, ctypes.POINTER(POINT)]
    user32.ClientToScreen.argtypes = [HWND, ctypes.POINTER(POINT)]
    user32.EnumChildWindows.argtypes = [HWND, c_void_p, LPARAM]
    user32.PostMessageW.argtypes = [HWND, UINT, WPARAM, LPARAM]
    user32.SendMessageW.argtypes = [HWND, UINT, WPARAM, LPARAM]
    user32.SetForegroundWindow.argtypes = [HWND]
    user32.SetFocus.argtypes = [HWND]
    user32.GetFocus.restype = HWND
    user32.GetForegroundWindow.restype = HWND
    user32.GetWindowThreadProcessId.argtypes = [HWND, c_void_p]
    user32.GetWindowThreadProcessId.restype = DWORD
    user32.AttachThreadInput.argtypes = [DWORD, DWORD, BOOL]
    user32.AttachThreadInput.restype = BOOL

_set_proto()

def read_text(hwnd) -> str:
    if not hwnd:
        return ""
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        buf = ctypes.create_unicode_buffer(1)
        user32.GetWindowTextW(hwnd, buf, 1)
        return buf.value
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value

def _class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value

def find_child_windows(hwnd_top) -> list:
    """EnumChildWindows 全列子窗（含非 Edit）。"""
    out = []

    @WINFUNCTYPE(BOOL, HWND, LPARAM)
    def _cb(hwnd, lparam):
        r = RECT()
        user32.GetWindowRect(hwnd, byref(r))
        out.append({
            "hwnd": int(hwnd),
            "class": _class_name(hwnd),
            "rect": [r.left, r.top, r.right, r.bottom],
            "visible": bool(user32.IsWindowVisible(hwnd)),
            "text": read_text(hwnd),
        })
        return True

    user32.EnumChildWindows(hwnd_top, _cb, 0)
    return out

def find_edit_child_fast(hwnd_top):
    """冻结桌面安全的 Edit 子窗查找：只走 GetClassNameW/IsWindowVisible
    （内核态，不发跨线程消息）；不做 GetWindowTextW/GetWindowRect。
    返回首个可见 Edit 的 hwnd，无则 None。
    """
    out = []

    @WINFUNCTYPE(BOOL, HWND, LPARAM)
    def _cb(hwnd, lparam):
        out.append((int(hwnd), _class_name(hwnd),
                    bool(user32.IsWindowVisible(hwnd))))
        return True

    user32.EnumChildWindows(hwnd_top, _cb, 0)
    for h, cls, vis in out:
        if cls == "Edit" and vis:
            return h
    return None

def focus_window(hwnd_top, hwnd_target) -> dict:
    """SetForegroundWindow+SetFocus，失败时 AttachThreadInput 重试一次。"""
    if not hwnd_top or not hwnd_target:
        return {"ok": False, "method": "none", "note": "missing hwnd"}
    attempts = []

    # 直接尝试
    ok_fg = bool(user32.SetForegroundWindow(hwnd_top))
    user32.SetFocus(hwnd_target)
    if user32.GetFocus() == hwnd_target:
        return {"ok": True, "method": "direct"}
    attempts.append({"try": "direct", "set_fg": ok_fg,
                     "focus_hwnd": int(user32.GetFocus() or 0)})

    # AttachThreadInput 重试
    fg = user32.GetForegroundWindow()
    tid_cur = kernel32.GetCurrentThreadId()
    tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    tid_top = user32.GetWindowThreadProcessId(hwnd_top, None)
    attached = []
    try:
        for tid in {tid_fg, tid_top}:
            if tid and tid != tid_cur:
                if user32.AttachThreadInput(tid_cur, tid, True):
                    attached.append(tid)
        user32.SetForegroundWindow(hwnd_top)
        user32.SetFocus(hwnd_target)
        if user32.GetFocus() == hwnd_target:
            return {"ok": True, "method": "attach_thread_input",
                    "attempts": attempts, "attached": attached}
    finally:
        for tid in attached:
            user32.AttachThreadInput(tid_cur, tid, False)
    attempts.append({"try": "attach", "focus_hwnd": int(user32.GetFocus() or 0)})
    return {"ok": False, "method": "none", "attempts": attempts}

def _resolve_order(order):
    if order is not None:
        return list(order)
    env = (os.environ.get("INPUT_FALLBACK") or "auto").strip().lower()
    if env == "sendinput":
        return ["sendinput"]
    if env == "postmessage":
        return ["postmessage"]
    if env == "sendmessage":
        return ["sendmessage"]
    if env == "hook":
        return ["hook"]
    return ["sendinput", "postmessage", "sendmessage", "hook"]

def _parse_sendinput_err(e: Exception):
    msg = str(e)
    sent, total, err = 0, 0, 0
    m = _PM_RE.search(msg)
    if m:
        sent = int(m.group(1)); total = int(m.group(2))
    m = _ERR_RE.search(msg)
    if m:
        err = int(m.group(1))
    return sent, total, err

def _try_sendinput_text(text: str):
    """调 win_input.type_text，返回 (sent_events, winerror)。"""
    n_ev = len(text) * 2
    try:
        win_input.type_text(text, per_char_delay_ms=0)
        return n_ev, 0
    except RuntimeError as e:
        sent, _total, err = _parse_sendinput_err(e)
        return sent, err

def _try_sendinput_click(x: int, y: int, button: str = "left"):
    try:
        win_input.click(x, y)
        return 2, 0
    except RuntimeError as e:
        sent, _total, err = _parse_sendinput_err(e)
        return sent, err

def _target_window(hwnd_top, hwnd_target):
    return hwnd_target or hwnd_top

def type_chain(text, hwnd_top=None, hwnd_target=None, order=None) -> dict:
    levels = []
    target = _target_window(hwnd_top, hwnd_target)
    for method in _resolve_order(order):
        lv = {"method": method, "sent": 0, "winerror": None,
              "ms": 0, "ok": False, "readback": "", "note": ""}
        delivered = False
        t0 = time.time()
        try:
            if method == "sendinput":
                sent, err = _try_sendinput_text(text)
                lv["sent"] = sent
                lv["winerror"] = err
                # 全部字符发送成功才算送达；被拒(winerror)/部分失败 → 落下一级
                delivered = (err is None) and (sent == len(text))
            elif method == "postmessage":
                if not target:
                    lv["note"] = "no target hwnd"
                else:
                    ok_posts = 0
                    for ch in text:
                        if user32.PostMessageW(target, WM_CHAR, ord(ch), 0):
                            ok_posts += 1
                    lv["sent"] = ok_posts
                    delivered = ok_posts == len(text)
            elif method == "sendmessage":
                if not target:
                    lv["note"] = "no target hwnd"
                else:
                    for ch in text:
                        user32.SendMessageW(target, WM_CHAR, ord(ch), 0)
                    # 同步调用：返回即窗口已处理（送达）
                    delivered = True
            elif method == "hook":
                # 钩子级：F3 已证同会话 SendInput 被拒；钩子过程里也是 SendInput，
                # 成功率不高。为避免引入消息循环复杂度，这里如实记为未启用。
                lv["note"] = "hook level deferred: same-session SendInput denied (F3)"
        except Exception as e:
            lv["note"] = f"exception: {e}"
        time.sleep(0.1)
        lv["readback"] = read_text(target) if target else ""
        lv["ms"] = int((time.time() - t0) * 1000)
        rb = lv["readback"] or ""
        # M6 #5（用户裁决）：送达即停——本级已成功投递（sendinput 全成功 /
        # postmessage 全部入队 / sendmessage 同步完成）就不再落下一级重发；
        # 旧逻辑只认"读回包含所发文本"，chrome 等读不回内容的窗口（readback
        # 是窗口标题）永远不 landed → 逐级重发 → 用户输入翻倍
        # （"亚运会亚运会"）。仅当本级确定未送达时才落下一级。
        lv["ok"] = bool(target) and (
            delivered or (text in rb) or rb.endswith(text))
        levels.append(lv)
        if lv["ok"]:
            return {"method": method, "landed": True,
                    "readback": rb, "levels": levels}
    rb = levels[-1]["readback"] if levels else ""
    return {"method": None, "landed": False, "readback": rb, "levels": levels}

def click_chain(x, y, hwnd_top=None, hwnd_target=None,
                button: str = "left", double: bool = False, order=None) -> dict:
    levels = []
    target = _target_window(hwnd_top, hwnd_target)
    down = WM_LBUTTONDOWN if button == "left" else WM_RBUTTONDOWN
    up = WM_LBUTTONUP if button == "left" else WM_RBUTTONUP
    mk = MK_LBUTTON if button == "left" else MK_RBUTTON
    for method in _resolve_order(order):
        lv = {"method": method, "sent": 0, "winerror": None,
              "ms": 0, "ok": False, "note": ""}
        t0 = time.time()
        try:
            if method == "sendinput":
                sent, err = _try_sendinput_click(x, y, button)
                lv["sent"] = sent
                lv["winerror"] = err
            elif method in ("postmessage", "sendmessage"):
                if not target:
                    lv["note"] = "no target hwnd"
                else:
                    pt = POINT(x, y)
                    user32.ScreenToClient(target, byref(pt))
                    lp = ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)
                    fn = (user32.PostMessageW if method == "postmessage"
                          else user32.SendMessageW)
                    try:
                     user32.SetCursorPos(int(x), int(y))
                     _set_last_cursor(int(x), int(y))
                    except Exception:
                     pass
                    fn(target, WM_MOUSEMOVE, 0, lp)
                    reps = 2 if double else 1
                    for _i in range(reps):
                     fn(target, down, mk, lp)
                     fn(target, up, 0, lp)
                     if double and _i + 1 < reps:
                      time.sleep(0.03)
            elif method == "hook":
                lv["note"] = "hook level deferred (see type_chain)"
        except Exception as e:
            lv["note"] = f"exception: {e}"
        time.sleep(0.1)
        lv["ms"] = int((time.time() - t0) * 1000)
        # 鼠标落地不易读回，只能以 winerror/no-exception 判定
        lv["ok"] = (lv["winerror"] == 0) or (
            method in ("postmessage", "sendmessage") and not lv["note"])
        levels.append(lv)
        if lv["ok"]:
            return {"method": method, "landed": True, "levels": levels}
    return {"method": None, "landed": False, "levels": levels}


def key_chain(key_name: str, hwnd_top=None, hwnd_target=None,
              repeat: int = 1, order=None) -> dict:
    """press one key: sendinput -> postmessage -> sendmessage."""
    levels = []
    target = _target_window(hwnd_top, hwnd_target)
    try:
        vk = win_input._vk(key_name)
    except Exception as e:
        return {"ok": False, "landed": False, "levels": [],
                "error": "unknown key %r: %r" % (key_name, e)}
    reps = max(1, int(repeat))
    for method in _resolve_order(order):
        lv = {"method": method, "sent": 0, "winerror": None,
              "ms": 0, "ok": False, "note": ""}
        t0 = time.time()
        try:
            if method == "sendinput":
                win_input.press_key(key_name, repeat=reps)
                lv["sent"] = reps
            elif method in ("postmessage", "sendmessage"):
                if not target:
                    lv["note"] = "no target hwnd"
                else:
                    fn = (user32.PostMessageW if method == "postmessage"
                          else user32.SendMessageW)
                    for _i in range(reps):
                        fn(target, WM_KEYDOWN, vk, 0)
                        fn(target, WM_KEYUP, vk, 0)
                    lv["sent"] = reps
            elif method == "hook":
                lv["note"] = "hook deferred"
        except Exception as e:
            lv["note"] = "exception: %r" % (e,)
        time.sleep(0.1)
        lv["ms"] = int((time.time() - t0) * 1000)
        lv["ok"] = not lv["note"]
        levels.append(lv)
        if lv["ok"]:
            return {"method": method, "landed": True, "levels": levels, "vk": vk}
    return {"method": None, "landed": False, "levels": levels, "vk": vk}


def hotkey_chain(keys, hwnd_top=None, hwnd_target=None, order=None) -> dict:
    """hotkey: sendinput -> postmessage -> sendmessage."""
    levels = []
    target = _target_window(hwnd_top, hwnd_target)
    if not keys:
        return {"ok": False, "landed": False, "levels": [],
                "error": "hotkey needs at least one key"}
    try:
        vks = [win_input._vk(k) for k in keys]
    except Exception as e:
        return {"ok": False, "landed": False, "levels": [],
                "error": "unknown key in %r: %r" % (keys, e)}
    for method in _resolve_order(order):
        lv = {"method": method, "sent": 0, "winerror": None,
              "ms": 0, "ok": False, "note": ""}
        t0 = time.time()
        try:
            if method == "sendinput":
                win_input.hotkey(list(keys))
                lv["sent"] = len(keys)
            elif method in ("postmessage", "sendmessage"):
                if not target:
                    lv["note"] = "no target hwnd"
                else:
                    fn = (user32.PostMessageW if method == "postmessage"
                          else user32.SendMessageW)
                    for vk in vks[:-1]:
                        fn(target, WM_KEYDOWN, vk, 0)
                    fn(target, WM_KEYDOWN, vks[-1], 0)
                    fn(target, WM_KEYUP, vks[-1], 0)
                    for vk in reversed(vks[:-1]):
                        fn(target, WM_KEYUP, vk, 0)
                    lv["sent"] = len(keys)
            elif method == "hook":
                lv["note"] = "hook deferred"
        except Exception as e:
            lv["note"] = "exception: %r" % (e,)
        time.sleep(0.1)
        lv["ms"] = int((time.time() - t0) * 1000)
        lv["ok"] = not lv["note"]
        levels.append(lv)
        if lv["ok"]:
            return {"method": method, "landed": True, "levels": levels, "vks": vks}
    return {"method": None, "landed": False, "levels": levels, "vks": vks}


def set_text(hwnd_target, text: str) -> dict:
    """跨进程 SetWindowTextW 直塞文本（替换语义，非追加）。

    用于经典 Edit 类子窗；跨进程安全，不依赖焦点。
    返回 {ok, sent, readback, winerror}，ok = 读回包含所发文本。
    """
    if not hwnd_target:
        return {"ok": False, "sent": text, "readback": "",
                "winerror": None, "note": "no target hwnd"}
    user32.SetWindowTextW.argtypes = [c_void_p, ctypes.c_wchar_p]
    user32.SetWindowTextW.restype = ctypes.c_int
    ret = user32.SetWindowTextW(hwnd_target, text)
    winerror = kernel32.GetLastError()
    time.sleep(0.1)
    rb = read_text(hwnd_target)
    return {"ok": bool(ret) and (text in rb), "sent": text,
            "readback": rb, "winerror": winerror, "method": "setwindowtext"}


def mouse_chain(action, x, y, hwnd_top=None, hwnd_target=None,
                   button="left", double=False,
                   x2=0, y2=0, steps=12, delta=0, order=None):
    """mouse action fallback: sendinput -> postmessage -> sendmessage."""
    levels = []
    target = _target_window(hwnd_top, hwnd_target)
    WM_MOUSEMOVE = 0x0200
    WM_MOUSEWHEEL = 0x020A
    WM_RDOWN = 0x0204
    WM_RUP = 0x0205
    WM_MDOWN = 0x0207
    WM_MUP = 0x0208
    for method in _resolve_order(order):
        lv = {"method": method, "sent": 0, "winerror": None,
              "ms": 0, "ok": False, "note": ""}
        t0 = time.time()
        try:
            if method == "sendinput":
                if action == "move":
                    win_input.move_to(x, y)
                elif action in ("click", "double_click", "right_click", "middle_click"):
                    btn = {"click": "left", "double_click": "left", "right_click": "right", "middle_click": "middle"}[action]
                    win_input.click(x, y, button=btn, double=(action == "double_click"))
                elif action == "drag":
                    win_input.drag(x, y, x2, y2, steps=steps)
                elif action == "scroll":
                    win_input.scroll(x, y, delta)
            elif method in ("postmessage", "sendmessage"):
                if not target:
                    lv["note"] = "no target hwnd"
                else:
                    fn = (user32.PostMessageW if method == "postmessage" else user32.SendMessageW)
                    pt = POINT(x, y)
                    user32.ScreenToClient(target, byref(pt))
                    lp = ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)
                    if action == "move":
                        try:
                         user32.SetCursorPos(int(x), int(y))
                         _set_last_cursor(int(x), int(y))
                        except Exception:
                         pass
                        fn(target, WM_MOUSEMOVE, 0, lp)
                    elif action in ("click", "double_click", "right_click", "middle_click"):
                        button_map = {"click": (WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON),
                                    "double_click": (WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON),
                                    "right_click": (WM_RDOWN, WM_RUP, MK_RBUTTON),
                                    "middle_click": (WM_MDOWN, WM_MUP, 0x0010)}
                        dn, up, mk = button_map[action]
                        reps = 2 if action == "double_click" else 1
                        for _i in range(reps):
                            _set_last_cursor(int(x), int(y))
                            fn(target, dn, mk, lp)
                            fn(target, up, 0, lp)
                            if action == "double_click" and _i + 1 < reps:
                                time.sleep(0.03)
                    elif action == "drag":
                        pt2 = POINT(x2, y2)
                        user32.ScreenToClient(target, byref(pt2))
                        lp2 = ((pt2.y & 0xFFFF) << 16) | (pt2.x & 0xFFFF)
                        fn(target, WM_LBUTTONDOWN, MK_LBUTTON, lp)
                        sn = max(2, int(steps))
                        for i in range(1, sn + 1):
                            cx = int(round(x + (x2 - x) * i / sn))
                            cy = int(round(y + (y2 - y) * i / sn))
                            cp = POINT(cx, cy)
                            user32.ScreenToClient(target, byref(cp))
                            cpl = ((cp.y & 0xFFFF) << 16) | (cp.x & 0xFFFF)
                            fn(target, WM_MOUSEMOVE, MK_LBUTTON, cpl)
                        fn(target, WM_LBUTTONUP, 0, lp2)
                        _set_last_cursor(int(x2), int(y2))
                    elif action == "scroll":
                        wparam = ((delta * 120) & 0xFFFF) << 16
                        lparam_sc = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
                        fn(target, WM_MOUSEWHEEL, wparam, lparam_sc)
                        _set_last_cursor(int(x), int(y))
            elif method == "hook":
                lv["note"] = "hook deferred"
        except Exception as e:
            lv["note"] = "exception: %r" % (e,)
        time.sleep(0.05)
        lv["ms"] = int((time.time() - t0) * 1000)
        lv["ok"] = not lv["note"]
        levels.append(lv)
        if lv["ok"]:
            return {"method": method, "landed": True, "levels": levels}
    return {"method": None, "landed": False, "levels": levels}

