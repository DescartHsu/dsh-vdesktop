"""dshv.cli — M0 acceptance demo commands (docs/gui-agent-plan/06b).

Usage (run from sidecar/ or anywhere with dshv importable):

  python -m dshv.cli demo create-launch --app notepad.exe
  python -m dshv.cli demo list-windows --name "dshv-*"
  python -m dshv.cli demo shot --name "dshv-*" --out m0.jpg
  python -m dshv.cli demo type --name "dshv-*" --text "你好 Hello"
  python -m dshv.cli demo click --name "dshv-*" --x 100 --y 60
  python -m dshv.cli demo key --name "dshv-*" --key Escape
  python -m dshv.cli demo read-text --name "dshv-*"
  python -m dshv.cli demo close --name "dshv-*"
  python -m dshv.cli demo orphan-sweep

Every command prints one JSON object on stdout. Exit code 0 = ok, 2 = usage/
lookup error, 1 = runtime failure. Timings (elapsedMs) are included so the
acceptance script can build a performance baseline.
"""

import argparse
import ctypes
import fnmatch
import json
import os
import random
import string
import sys
import time

from .win32 import capture_dual
from .win32 import capture_gdi
from .win32 import desktop as win_desktop
from .win32 import dpi as win_dpi
from .win32 import input as win_input
from .win32 import process as win_process
from .win32.defs import DESKTOP_ALL, DESKTOP_ENUMERATE, DESKTOP_READ_OBJECTS


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _fail(code: int, message: str, **extra) -> int:
    _emit({"ok": False, "error": message, **extra})
    return code


def _new_name() -> str:
    return "dshv-" + "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(6))


def _match_desktops(pattern: str):
    names = win_desktop.enumerate_desktops()
    hits = [n for n in names if fnmatch.fnmatch(n, pattern)]
    return hits


def _open(name: str, access: int = DESKTOP_READ_OBJECTS | DESKTOP_ENUMERATE):
    h = win_desktop.open_desktop(name, access)
    if not h:
        raise RuntimeError(f"OpenDesktopW({name!r}) failed")
    return h


def _require_single(pattern: str) -> str:
    hits = _match_desktops(pattern)
    if not hits:
        raise LookupError(f"no desktop matches {pattern!r} (have: {win_desktop.enumerate_desktops()})")
    if len(hits) > 1:
        # deterministic: alphabetical; M0 flow creates exactly one dshv-* desktop
        hits.sort()
    return hits[0]


def _windows_on(h) -> list[dict]:
    out = []
    for hwnd in win_process.enumerate_desktop_windows(h):
        info = win_process.window_info(hwnd)
        if not info["visible"]:
            continue
        pid = 0
        try:
            from ctypes import byref, wintypes
            got = wintypes.DWORD(0)
            win_desktop.user32.GetWindowThreadProcessId(hwnd, byref(got))
            pid = got.value
        except Exception:  # noqa: BLE001
            pid = 0
        out.append({"hwnd": hwnd, "pid": pid, **info})
    return out


# --------------------------------------------------------------- commands --


def cmd_create_launch(a) -> int:
    t0 = time.perf_counter()
    name = a.name or _new_name()
    t_create = time.perf_counter()
    try:
        h = win_desktop.create_desktop(name)
    except win_desktop.DesktopError as e:
        return _fail(1, f"CreateDesktopW failed: {e} (winerror={e.winerror})", desktopName=name)
    create_ms = int((time.perf_counter() - t_create) * 1000)
    t_launch = time.perf_counter()
    try:
        res = win_process.create_process_on_desktop(
            name, a.app, args=a.args or "", wait_window_ms=5000
        )
    except win_desktop.DesktopError as e:
        win_desktop.close_desktop(h)
        return _fail(1, f"CreateProcessW failed: {e} (winerror={e.winerror})", desktopName=name)
    launch_ms = int((time.perf_counter() - t_launch) * 1000)
    total_ms = int((time.perf_counter() - t0) * 1000)
    _emit({
        "ok": True,
        "desktopName": name,
        "hDesktop": int(h),
        "pid": res["pid"],
        "hwnd": res["hwnd"],
        "title": res["title"],
        "windowReady": res["windowReady"],
        "windowRect": res["windowRect"],
        "createMs": create_ms,
        "launchMs": launch_ms,
        "elapsedMs": total_ms,
    })
    return 0


def cmd_list_windows(a) -> int:
    hits = _match_desktops(a.name)
    if not hits:
        return _fail(2, f"no desktop matches {a.name!r}", available=win_desktop.enumerate_desktops())
    out = []
    for name in hits:
        h = _open(name)
        try:
            wins = _windows_on(h)
        finally:
            win_desktop.close_desktop(h)
        out.append({"desktopName": name, "windows": wins})
    _emit({"ok": True, "desktops": out})
    return 0


def cmd_shot(a) -> int:
    t0 = time.perf_counter()
    name = _require_single(a.name)
    h = _open(name, DESKTOP_ALL)
    try:
        win_desktop.set_thread_desktop(h)
        res = capture_dual.capture_dual(
            a.out,
            max_long_edge=a.max_edge,
            fmt="jpeg",
            quality=a.quality,
            region=None,
            wait_after_attach=0.8,
        )
    except Exception as e:  # noqa: BLE001
        return _fail(1, f"capture failed: {e}", desktopName=name)
    finally:
        win_desktop.close_desktop(h)
    res["desktopName"] = name
    res["elapsedMs"] = int((time.perf_counter() - t0) * 1000)
    _emit({"ok": True, **res})
    return 0


def cmd_type(a) -> int:
    t0 = time.perf_counter()
    name = _require_single(a.name)
    h = _open(name, DESKTOP_ALL)
    try:
        win_desktop.set_thread_desktop(h)
        _fb = (os.environ.get("INPUT_FALLBACK") or "auto").strip().lower()
        if _fb == "sendinput":
            res = win_input.type_text(a.text)
        else:
            # D2 降级链（大脑 4.2）：非 sendinput 时走 input_chain，
            # 目标 = 第一个非系统顶层窗 + 其第一个可见 Edit 子窗（WinUI 用顶层窗）
            from .win32 import input_chain as ic

            wins = _windows_on(h)
            hwnd_top = None
            for w in wins:
                if w["class"] in ("Progman", "WorkerW", "Shell_TrayWnd"):
                    continue
                hwnd_top = w["hwnd"]
                break
            hwnd_target = None
            if hwnd_top:
                for c in ic.find_child_windows(hwnd_top):
                    if c["class"] == "Edit" and c["visible"]:
                        hwnd_target = c["hwnd"]
                        break
            # M0：目标为经典 Edit 子窗时，优先 SetWindowTextW 直塞（跨进程安全、不依赖焦点）
            res = None
            _is_edit_child = False
            if hwnd_target and hwnd_target != hwnd_top:
                try:
                    _cn = ctypes.create_unicode_buffer(64)
                    ctypes.windll.user32.GetClassNameW(hwnd_target, _cn, 64)
                    _is_edit_child = (_cn.value == "Edit")
                except Exception:
                    pass
            if _is_edit_child:
                _r = ic.set_text(hwnd_target, a.text)
                if _r.get("ok"):
                    res = _r
            if res is None:
                res = ic.type_chain(a.text, hwnd_top=hwnd_top, hwnd_target=hwnd_target)
    except Exception as e:  # noqa: BLE001
        return _fail(1, f"type failed: {e}", desktopName=name)
    finally:
        win_desktop.close_desktop(h)
    res["desktopName"] = name
    res["elapsedMs"] = int((time.perf_counter() - t0) * 1000)
    _emit({"ok": True, **res})
    return 0


def cmd_click(a) -> int:
    t0 = time.perf_counter()
    name = _require_single(a.name)
    h = _open(name, DESKTOP_ALL)
    try:
        win_desktop.set_thread_desktop(h)
        _fb = (os.environ.get("INPUT_FALLBACK") or "auto").strip().lower()
        if _fb == "sendinput" and not a.double:
            res = win_input.click(a.x, a.y, button=a.button, double=a.double)
        else:
            # D2 降级链（大脑 4.3）：click_chain（含 sendinput 级）
            from .win32 import input_chain as ic

            wins = _windows_on(h)
            hwnd_top = None
            for w in wins:
                if w["class"] in ("Progman", "WorkerW", "Shell_TrayWnd"):
                    continue
                hwnd_top = w["hwnd"]
                break
            hwnd_target = None
            if hwnd_top:
                for c in ic.find_child_windows(hwnd_top):
                    if c["class"] == "Edit" and c["visible"]:
                        hwnd_target = c["hwnd"]
                        break
            res = ic.click_chain(
                a.x, a.y,
                hwnd_top=hwnd_top, hwnd_target=hwnd_target,
                button=a.button,
            )
    except Exception as e:  # noqa: BLE001
        return _fail(1, f"click failed: {e}", desktopName=name)
    finally:
        win_desktop.close_desktop(h)
    res["desktopName"] = name
    res["elapsedMs"] = int((time.perf_counter() - t0) * 1000)
    _emit({"ok": True, **res})
    return 0


def cmd_key(a) -> int:
    t0 = time.perf_counter()
    name = _require_single(a.name)
    h = _open(name, DESKTOP_ALL)
    try:
        win_desktop.set_thread_desktop(h)
        if a.hotkey:
            res = win_input.hotkey([k.strip() for k in a.hotkey.split(",") if k.strip()])
        else:
            res = win_input.press_key(a.key)
    except Exception as e:  # noqa: BLE001
        return _fail(1, f"key failed: {e}", desktopName=name)
    finally:
        win_desktop.close_desktop(h)
    res["desktopName"] = name
    res["elapsedMs"] = int((time.perf_counter() - t0) * 1000)
    _emit({"ok": True, **res})
    return 0


def cmd_read_text(a) -> int:
    """Best-effort text readback of the main app window's edit control.

    Win10/old notepad: top-level class 'Notepad' with an 'Edit' child.
    Returns {"text": str-or-None, "editHwnd": int-or-None, ...}.
    """
    name = _require_single(a.name)
    h = _open(name)
    # 大脑 5e02e172ac7f：线程必须在目标桌面上，EnumChildWindows 才能枚举子窗（不切回）
    win_desktop.set_thread_desktop(h)
    try:
        wins = _windows_on(h)
    finally:
        win_desktop.close_desktop(h)
    app = None
    for w in wins:
        if w["class"] in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            continue
        app = w
        break
    if app is None:
        return _fail(2, "no app window found on desktop", desktopName=name)
    from ctypes import byref, create_unicode_buffer, windll, wintypes

    from .win32.defs import WNDENUMPROC

    user32 = windll.user32
    user32.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]
    user32.EnumChildWindows.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, wintypes.INT]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, wintypes.INT]
    user32.GetWindowTextW.restype = wintypes.INT

    edits: list = []

    def _cb(hwnd, _l) -> bool:
        cls = create_unicode_buffer(260)
        user32.GetClassNameW(hwnd, cls, 260)
        if cls.value == "Edit":
            edits.append(hwnd)
        return True

    user32.EnumChildWindows(app["hwnd"], WNDENUMPROC(_cb), 0)
    text = None
    edit_hwnd = edits[0] if edits else 0
    if edit_hwnd:
        buf = create_unicode_buffer(32768)
        if user32.GetWindowTextW(edit_hwnd, buf, 32768) > 0:
            text = buf.value
    if edit_hwnd:
        source = "child_edit"
    else:
        # D2 兼容（大脑 4.4）：WinUI 新 notepad 无 Edit 子窗 → 读顶层窗文本
        from .win32 import input_chain as ic

        text = ic.read_text(app["hwnd"])
        source = "top_window"
    _emit({
        "ok": True,
        "desktopName": name,
        "windowClass": app["class"],
        "windowTitle": app["title"],
        "hwnd": app["hwnd"],
        "editHwnd": edit_hwnd or None,
        "text": text,
        "source": source,
    })
    return 0


def cmd_close(a) -> int:
    t0 = time.perf_counter()
    name = _require_single(a.name)
    h = _open(name, DESKTOP_ALL)
    if not h:
        return _fail(1, f"desktop {name!r} not found (already closed?)")
    try:
        pids = sorted(win_process.desktop_processes(h))
        killed = win_process.terminate_pids(pids) if pids else {}
        survivors = win_process.close_pids_gently(pids) if pids else set()
        closed = win_desktop.close_desktop(h)
        if not win_desktop.desktop_exists(name):
            closed = True
    except Exception as e:  # noqa: BLE001
        return _fail(1, f"close failed: {e}", desktopName=name)
    res = {
        "ok": True,
        "desktopName": name,
        "closed": bool(closed),
        "killed": killed,
        "survivors": sorted(survivors),
        "elapsedMs": int((time.perf_counter() - t0) * 1000),
    }
    _emit(res)
    return 0


def cmd_orphan_sweep(a) -> int:
    t0 = time.perf_counter()
    hits = [n for n in win_desktop.enumerate_desktops()
            if n == a.prefix or n.startswith(a.prefix)]
    swept = []
    for name in hits:
        entry = {"desktopName": name}
        try:
            h = _open(name, DESKTOP_ALL)
        except RuntimeError:
            entry.update({"closed": False, "reason": "open failed"})
            swept.append(entry)
            continue
        try:
            pids = sorted(win_process.desktop_processes(h))
            if pids:
                win_process.terminate_pids(pids)
                win_process.close_pids_gently(pids)
            closed = win_desktop.close_desktop(h)
            if not win_desktop.desktop_exists(name):
                closed = True
            entry.update({"closed": bool(closed), "killedPids": pids})
        except Exception as e:  # noqa: BLE001
            entry.update({"closed": False, "reason": str(e)})
        swept.append(entry)
    remaining = [n for n in win_desktop.enumerate_desktops()
                 if n == a.prefix or n.startswith(a.prefix)]
    _emit({
        "ok": True,
        "prefix": a.prefix,
        "swept": swept,
        "sweptCount": len(swept),
        "remaining": remaining,
        "remainingCount": len(remaining),
        "elapsedMs": int((time.perf_counter() - t0) * 1000),
    })
    return 0


# ------------------------------------------------------------------ main --


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dshv.cli", description=__doc__)
    groups = p.add_subparsers(dest="group", required=True)
    demo = groups.add_parser("demo", help="M0 acceptance demo commands")
    subs = demo.add_subparsers(dest="cmd", required=True)

    c = subs.add_parser("create-launch", help="create dshv-* desktop and launch app on it")
    c.add_argument("--app", required=True, help="exe path (e.g. notepad.exe or C:\\Windows\\notepad.exe)")
    c.add_argument("--args", default="", help="app command line args")
    c.add_argument("--name", default=None, help="fixed desktop name (default: auto dshv-<6>)")
    c.set_defaults(fn=cmd_create_launch)

    c = subs.add_parser("list-windows", help="list visible top-level windows on matching desktops")
    c.add_argument("--name", required=True, help='glob, e.g. "dshv-*"')
    c.set_defaults(fn=cmd_list_windows)

    c = subs.add_parser("shot", help="GDI screenshot of a non-foreground desktop "
                     "(CAPTURE_CHANNEL=auto|display|printwindow, default auto: "
                     "display DC with black-frame gate, auto-fallback to PrintWindow)")
    c.add_argument("--name", required=True, help='glob, e.g. "dshv-*"')
    c.add_argument("--out", required=True, help="output file path (.jpg)")
    c.add_argument("--max-edge", type=int, default=1280)
    c.add_argument("--quality", type=int, default=80)
    c.set_defaults(fn=cmd_shot)

    c = subs.add_parser("type", help="type unicode text to the attached desktop")
    c.add_argument("--name", required=True)
    c.add_argument("--text", required=True)
    c.set_defaults(fn=cmd_type)

    c = subs.add_parser("click", help="mouse click on the attached desktop")
    c.add_argument("--name", required=True)
    c.add_argument("--x", type=int, required=True)
    c.add_argument("--y", type=int, required=True)
    c.add_argument("--button", default="left", choices=["left", "right", "middle"])
    c.add_argument("--double", action="store_true")
    c.set_defaults(fn=cmd_click)

    c = subs.add_parser("key", help="press a key / hotkey on the attached desktop")
    c.add_argument("--name", required=True)
    c.add_argument("--key", default=None, help='key name, e.g. Escape, enter, a')
    c.add_argument("--hotkey", default=None, help='comma list, e.g. "ctrl,s"')
    c.set_defaults(fn=cmd_key)

    c = subs.add_parser("read-text", help="read back the main app window's edit control text")
    c.add_argument("--name", required=True)
    c.set_defaults(fn=cmd_read_text)

    c = subs.add_parser("close", help="kill processes on the desktop and CloseDesktop")
    c.add_argument("--name", required=True)
    c.set_defaults(fn=cmd_close)

    c = subs.add_parser("orphan-sweep", help="close all dshv-* desktops (kill processes first)")
    c.add_argument("--prefix", default="dshv-", help="desktop name prefix to sweep")
    c.set_defaults(fn=cmd_orphan_sweep)

    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    p = _build_parser()
    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except LookupError as e:
        return _fail(2, str(e))
    except SystemExit as e:  # argparse errors
        return int(e.code or 0)


if __name__ == "__main__":
    sys.exit(main())