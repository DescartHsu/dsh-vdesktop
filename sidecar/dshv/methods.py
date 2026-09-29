"""Desktop-session operations.

Every function here that touches a desktop must run on a thread that has
called SetThreadDesktop() for the target desktop (the M0 CLI does it on the
main thread; the M1 RPC server has dedicated capture/input worker threads).
"""

import time

from .frames import FrameStore
from .win32 import capture_gdi, input as win_input, process as win_process
from .win32 import desktop as win_desktop
from .win32 import dpi as win_dpi
from .rpc import RpcError, E_CAPTURE_FAILED, E_INPUT_FAILED, E_DESKTOP_CREATE_FAILED, E_PROCESS_NOT_ON_DESKTOP

DEFAULT_MAX_LONG_EDGE = 1280
MAX_LONG_EDGE_MIN = 640
MAX_LONG_EDGE_MAX = 2048


class SessionContext:
    def __init__(self, session_id: str, desktop_name: str, h_desktop=0):
        self.session_id = session_id
        self.desktop_name = desktop_name
        self.h_desktop = h_desktop  # handle valid on worker threads
        self.frames = FrameStore(session_id)
        self.virtual = win_dpi.virtual_screen_rect()  # set on an attached thread
        self.created_at = time.time()
        self.last_capture = None  # dict from capture_desktop

    def describe(self) -> dict:
        return {
            "sessionId": self.session_id,
            "desktopName": self.desktop_name,
            "hDesktop": int(self.h_desktop or 0),
            "virtualScreen": list(self.virtual),
        }


# ------------------------------------------------------------------ ping ---


def do_ping(ctx: SessionContext, params: dict | None) -> dict:
    return {
        "pong": True,
        "desktop": ctx.desktop_name,
        "session": ctx.session_id,
        "pid": __import__("os").getpid(),
        "uptimeS": round(time.time() - ctx.created_at, 1),
    }


# ----------------------------------------------------------- screenshot ---


def do_screenshot(ctx: SessionContext, params: dict | None) -> dict:
    p = params or {}
    fmt = (p.get("format") or "jpeg").lower()
    quality = int(p.get("quality") or 80)
    max_edge = int(p.get("maxLongEdge") or DEFAULT_MAX_LONG_EDGE)
    max_edge = max(MAX_LONG_EDGE_MIN, min(MAX_LONG_EDGE_MAX, max_edge))
    region = p.get("region")
    if region is not None:
        region = (int(region[0]), int(region[1]), int(region[2]), int(region[3]))
    out = ctx.frames.next_path("jpg" if fmt == "jpeg" else "png")
    t0 = time.perf_counter()
    try:
        res = capture_gdi.capture_desktop(
            out_path=out,
            max_long_edge=max_edge,
            fmt=fmt,
            quality=quality,
            region=region,
        )
    except Exception as e:  # noqa: BLE001
        raise RpcError(E_CAPTURE_FAILED, f"capture failed: {e}") from e
    res["elapsedMs"] = int((time.perf_counter() - t0) * 1000)
    res["sessionId"] = ctx.session_id
    ctx.last_capture = res
    ctx.frames.prune()
    return res


# ----------------------------------------------------------------- input ---


def do_mouse(ctx: SessionContext, params: dict | None) -> dict:
    p = params or {}
    action = (p.get("action") or "move").lower()
    x, y = int(p.get("x") or 0), int(p.get("y") or 0)
    try:
        if action == "move":
            return win_input.move_to(x, y)
        if action in ("click", "double_click", "right_click", "middle_click"):
            button = {"click": "left", "double_click": "left", "right_click": "right", "middle_click": "middle"}[action]
            double = action == "double_click"
            return win_input.click(x, y, button=button, double=double)
        if action == "drag":
            x2, y2 = int(p.get("x2") or 0), int(p.get("y2") or 0)
            steps = int(p.get("steps") or 12)
            return win_input.drag(x, y, x2, y2, steps=steps)
        if action == "scroll":
            delta = int(p.get("delta") or 0)
            return win_input.scroll(x, y, delta)
        raise RpcError(E_INPUT_FAILED, f"unknown mouse action: {action}")
    except RpcError:
        raise
    except Exception as e:  # noqa: BLE001
        raise RpcError(E_INPUT_FAILED, f"mouse {action} failed: {e}") from e


def do_keyboard(ctx: SessionContext, params: dict | None) -> dict:
    p = params or {}
    action = (p.get("action") or "type").lower()
    try:
        if action == "type":
            text = p.get("text")
            if not isinstance(text, str):
                raise RpcError(E_INPUT_FAILED, "keyboard type: 'text' (string) required")
            delay = int(p.get("perCharDelayMs") or 0)
            return win_input.type_text(text, per_char_delay_ms=delay)
        if action == "press":
            key = p.get("key")
            if not key:
                raise RpcError(E_INPUT_FAILED, "keyboard press: 'key' required")
            repeat = int(p.get("repeat") or 1)
            return win_input.press_key(str(key), repeat=repeat)
        if action == "hotkey":
            keys = p.get("keys")
            if not keys:
                raise RpcError(E_INPUT_FAILED, "keyboard hotkey: 'keys' list required")
            return win_input.hotkey([str(k) for k in keys])
        raise RpcError(E_INPUT_FAILED, f"unknown keyboard action: {action}")
    except RpcError:
        raise
    except Exception as e:  # noqa: BLE001
        raise RpcError(E_INPUT_FAILED, f"keyboard {action} failed: {e}") from e


# --------------------------------------------------------------- launch ---


def do_launch(ctx: SessionContext, params: dict | None) -> dict:
    p = params or {}
    exe = p.get("exePath")
    if not exe:
        raise RpcError(E_INPUT_FAILED, "launch: 'exePath' required")
    args = p.get("args") or ""
    cwd = p.get("cwd") or None
    wait_ms = int(p.get("windowTimeoutMs") or (5000 if p.get("waitForWindow") else 0))
    if p.get("waitForWindow") is False:
        wait_ms = 0
    try:
        res = win_process.create_process_on_desktop(
            ctx.desktop_name, exe, args=args, cwd=cwd, wait_window_ms=wait_ms
        )
    except Exception as e:  # noqa: BLE001
        raise RpcError(E_DESKTOP_CREATE_FAILED, f"launch failed: {e}") from e
    res["desktop"] = ctx.desktop_name
    if wait_ms and not res["windowReady"]:
        raise RpcError(E_PROCESS_NOT_ON_DESKTOP, f"process {res['pid']} started but no window on desktop within {wait_ms}ms")
    return res


def do_terminate(ctx: SessionContext, params: dict | None) -> dict:
    p = params or {}
    h = ctx.h_desktop or win_desktop.open_desktop(ctx.desktop_name, win_desktop.DESKTOP_ALL)
    if not h:
        raise RpcError(E_SESSION_DEAD, f"desktop {ctx.desktop_name} not found")
    try:
        pids = [int(x) for x in (p.get("pids") or win_process.desktop_processes(h))]
        if not pids:
            return {"terminated": [], "survivors": []}
        killed = win_process.terminate_pids(pids)
        survivors = win_process.close_pids_gently(pids)
        return {"terminated": killed, "survivors": sorted(survivors)}
    finally:
        if h != ctx.h_desktop:
            win_desktop.close_desktop(h)


def do_close(ctx: SessionContext, params: dict | None) -> dict:
    """Fixed close order: kill processes -> wait -> CloseDesktop."""
    p = params or {}
    kill = p.get("killProcesses", True)
    keep = bool(p.get("keepDesktop", False))
    h = ctx.h_desktop or win_desktop.open_desktop(ctx.desktop_name, win_desktop.DESKTOP_ALL)
    if not h:
        return {"closed": False, "reason": "desktop already gone", "desktop": ctx.desktop_name}
    try:
        pids = list(win_process.desktop_processes(h)) if kill else []
        killed = win_process.terminate_pids(pids) if pids else {}
        survivors = win_process.close_pids_gently(pids) if pids else set()
        closed = False
        if not keep:
            closed = win_desktop.close_desktop(h)
            if not win_desktop.desktop_exists(ctx.desktop_name):
                closed = True
        ctx.h_desktop = 0
        return {
            "closed": closed,
            "killed": killed,
            "survivors": sorted(survivors),
            "desktop": ctx.desktop_name,
        }
    finally:
        if keep:
            pass
        elif h != ctx.h_desktop:
            win_desktop.close_desktop(h)


# ---------------------------------------------------------------- status ---


def do_status(ctx: SessionContext, params: dict | None) -> dict:
    h = ctx.h_desktop or win_desktop.open_desktop(ctx.desktop_name, win_desktop.DESKTOP_ALL)
    if not h:
        raise RpcError(E_SESSION_DEAD, f"desktop {ctx.desktop_name} not found")
    try:
        pids = sorted(win_process.desktop_processes(h))
        windows = []
        for hwnd in win_process.enumerate_desktop_windows(h):
            info = win_process.window_info(hwnd)
            if not info["visible"]:
                continue
            info["hwnd"] = hwnd
            windows.append(info)
            if len(windows) >= 20:
                break
        foreground = None
        try:
            from ctypes import windll

            fg = windll.user32.GetForegroundWindow()
            if fg:
                info = win_process.window_info(fg)
                info["hwnd"] = fg
                foreground = info
        except Exception:  # noqa: BLE001
            pass
        return {
            "desktop": ctx.desktop_name,
            "exists": True,
            "processes": pids,
            "windows": windows,
            "foreground": foreground,
            "virtualScreen": list(ctx.virtual),
        }
    finally:
        if h != ctx.h_desktop:
            win_desktop.close_desktop(h)