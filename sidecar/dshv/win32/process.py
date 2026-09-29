"""Process + window operations on a (possibly non-foreground) desktop.

All window queries are desktop-scoped via EnumDesktopWindows, which works on
desktops that are not currently visible — that is how we prove a process is
really running inside our virtual desktop.
"""

import time
from ctypes import byref, c_void_p, create_unicode_buffer, sizeof, windll, wintypes
from ctypes.wintypes import DWORD

from .defs import (
    CREATE_UNICODE_ENVIRONMENT,
    PROCESS_INFORMATION,
    PROCESS_QUERY_LIMITED_INFORMATION,
    PROCESS_TERMINATE,
    RECT,
    STARTUPINFOW,
    WNDENUMPROC,
)
from .desktop import DesktopError

user32 = windll.user32
kernel32 = windll.kernel32


def _last_error() -> int:
    return kernel32.GetLastError()


def create_process_on_desktop(
    desktop_name: str,
    exe_path: str,
    args: str = "",
    cwd: str | None = None,
    wait_window_ms: int = 0,
):
    """CreateProcessW with STARTUPINFO.lpDesktop = desktop_name.

    Returns dict(pid, hwnd, title, window_ready, elapsed_ms, window_rect).
    Child processes started by the child inherit the same desktop automatically.
    """
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPCWSTR,
        c_void_p,   # STARTUPINFOW* (wintypes lacks LPSTARTUPINFOW in CPython)
        c_void_p,   # PROCESS_INFORMATION*
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL

    si = STARTUPINFOW()
    si.cb = sizeof(si)
    si.lpDesktop = _wptr(desktop_name)
    pi = PROCESS_INFORMATION()

    cmdline = f'"{exe_path}"{(" " + args) if args else ""}'
    t0 = time.perf_counter()
    ok = kernel32.CreateProcessW(
        None,  # let Windows parse the exe path from the command line
        cmdline,
        None,
        None,
        False,
        0,
        None,
        cwd,
        byref(si),
        byref(pi),
    )
    if not ok:
        err = _last_error()
        raise DesktopError(f"CreateProcessW({cmdline!r}) failed (winerror={err})", err)

    from .desktop import open_desktop, close_desktop
    from .defs import DESKTOP_READ_OBJECTS, DESKTOP_ENUMERATE

    pid = pi.dwProcessId
    hwnd = 0
    title = ""
    rect = None
    # EnumDesktopWindows needs a desktop HANDLE, not the name.
    hdesk = open_desktop(desktop_name, DESKTOP_READ_OBJECTS | DESKTOP_ENUMERATE)
    deadline = time.perf_counter() + wait_window_ms / 1000.0 if wait_window_ms > 0 else 0
    try:
        if wait_window_ms > 0 and hdesk:
            while True:
                hwnd, title, rect = _find_first_window_of_pid(hdesk, pid)
                if hwnd:
                    break
                if time.perf_counter() >= deadline:
                    break
                time.sleep(0.2)
    finally:
        if hdesk:
            close_desktop(hdesk)
    kernel32.CloseHandle(pi.hProcess)
    kernel32.CloseHandle(pi.hThread)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return {
        "pid": pid,
        "hwnd": hwnd or None,
        "title": title or None,
        "windowReady": bool(hwnd),
        "windowRect": list(rect) if rect else None,
        "elapsedMs": elapsed_ms,
    }


def _wptr(s: str):
    from ctypes import c_wchar_p

    return c_wchar_p(s)


def enumerate_desktop_windows(desktop_handle):
    """Top-level HWNDs on the given desktop (int handle from OpenDesktopW)."""
    user32.EnumDesktopWindows.argtypes = [
        wintypes.HANDLE,
        WNDENUMPROC,
        wintypes.LPARAM,
    ]
    user32.EnumDesktopWindows.restype = wintypes.BOOL
    handles: list[int] = []

    def _cb(hwnd, _l) -> bool:
        handles.append(hwnd)
        return True

    cb = WNDENUMPROC(_cb)
    user32.EnumDesktopWindows(desktop_handle, cb, 0)
    return handles


def window_info(hwnd: int):
    """(class, title, visible, rect) for a window handle."""
    cls_buf = create_unicode_buffer(260)
    user32.GetClassNameW(hwnd, cls_buf, 260)
    title_buf = create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, title_buf, 512)
    r = RECT()
    user32.GetWindowRect(hwnd, byref(r))
    return {
        "class": cls_buf.value,
        "title": title_buf.value,
        "visible": bool(user32.IsWindowVisible(hwnd)),
        "rect": [r.left, r.top, r.right, r.bottom],
    }


def desktop_processes(desktop_handle):
    """Set of PIDs that own top-level windows on this desktop."""
    pids = set()
    for hwnd in enumerate_desktop_windows(desktop_handle):
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, byref(pid))
        if pid.value:
            pids.add(pid.value)
    return pids


_SHELL_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    "TaskListThumbnailWnd", "ForegroundStaging", "SysShadow",
}


def shell_pids() -> set:
    """桌面 shell 相关进程 PID 集合（explorer 等）+ 自身进程，不得被 terminate。

    GetShellWindow 拿到 explorer 的 shell 顶层窗（Progman/WorkerW），
    取其 PID 加入排除集。再排除自身进程。
    """
    import os as _os
    pids = set()
    try:
        shell_hwnd = user32.GetShellWindow()
        if shell_hwnd:
            pid = DWORD()
            user32.GetWindowThreadProcessId(shell_hwnd, byref(pid))
            if pid.value:
                pids.add(int(pid.value))
    except Exception:
        pass
    pids.add(_os.getpid())
    return pids


def window_info_fast(hwnd: int) -> dict:
    """轻量版窗口查询（大脑 1527ba854fe1 F1）：只做内核本地查询，
    禁 GetWindowTextW/GetWindowRect 等跨线程消息调用（冻结桌面会阻塞）。
    killable_pids 专用；其它调用点继续用原 window_info。
    """
    cls = ""
    vis = False
    pid = 0
    try:
        buf = create_unicode_buffer(260)
        user32.GetClassNameW(hwnd, buf, 260)
        cls = buf.value
    except Exception:
        cls = ""
    try:
        vis = bool(user32.IsWindowVisible(hwnd))
    except Exception:
        vis = False
    try:
        p = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, byref(p))
        pid = int(p.value or 0)
    except Exception:
        pid = 0
    return {"hwnd": hwnd, "class": cls, "visible": vis, "pid": pid}


def killable_pids(hdesk) -> set:
    """枚举桌面上所有顶层窗，收集 PID，过滤掉 shell 类与自身。

    与 desktop_processes 的差异：跳过 _SHELL_CLASSES 类名的顶层窗，
    并额外排除 shell_pids() 返回的 PID。用于所有"杀桌面上进程"路径。
    （大脑 1527ba854fe1 F1：改用 window_info_fast 本地查询，
    冻结桌面上 close 不再因 GetWindowTextW 挂死）
    """
    excluded = shell_pids()
    out = set()
    for h in enumerate_desktop_windows(hdesk):
        info = window_info_fast(h)
        if not info["class"]:
            continue
        if info["class"] in _SHELL_CLASSES:
            continue
        if not info["visible"]:
            continue
        pid = info["pid"]
        if pid and pid not in excluded:
            out.add(pid)
    return out


def _find_first_window_of_pid(desktop_handle, pid, timeout_s=1.0):
    """EnumDesktopWindows with a watchdog: conhost/cmd windows whose thread is
    blocked (e.g. waiting in ReadConsole) make EnumDesktopWindows hang on its
    internal SendMessage; a side thread + join(timeout) bounds the wait.
    """
    import threading

    res = {"v": (0, "", None)}

    def _run():
        try:
            for hwnd in enumerate_desktop_windows(desktop_handle):
                got = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, byref(got))
                if got.value == pid:
                    info = window_info(hwnd)
                    if info["visible"]:
                        res["v"] = (hwnd, info["title"], info["rect"])
                        return
        except Exception:
            pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout_s)
    return res["v"]


def terminate_pids(pids) -> dict:
    """TerminateProcess on each pid; returns {pid: exit_code_or_None}."""
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    results = {}
    for pid in pids:
        h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not h:
            results[pid] = None
            continue
        ok = bool(kernel32.TerminateProcess(h, 0))
        code = wintypes.DWORD(0)
        if ok:
            kernel32.GetExitCodeProcess(h, byref(code))
            results[pid] = code.value
        else:
            results[pid] = None
        kernel32.CloseHandle(h)
    return results


def close_pids_gently(pids, timeout_ms: int = 3000):
    """Terminate, wait, re-terminate anything still alive. Returns survivors."""
    killed = terminate_pids(pids)
    deadline = time.perf_counter() + timeout_ms / 1000.0
    alive = set(pids)
    while alive and time.perf_counter() < deadline:
        still = set()
        for pid in alive:
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if h:
                code = wintypes.DWORD(0xFFFFFFFF)
                ok = kernel32.GetExitCodeProcess(h, byref(code))
                kernel32.CloseHandle(h)
                if ok and code.value != 0xFFFFFFFF:
                    continue  # exited
                still.add(pid)
            else:
                # open failed -> likely exited (or access issue); treat as exited
                pass
        alive = still
        if alive:
            terminate_pids(alive)
            time.sleep(0.3)
    return alive