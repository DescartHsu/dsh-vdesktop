"""Virtual-desktop (HDESK) management.

A "virtual desktop" here is the classic Win32 HDESK object created with
CreateDesktopW inside the current window station (WinSta0 by default, same
user, no elevation required).
"""

from ctypes import sizeof, windll, wintypes

from .defs import DESKTOP_ALL, DESKTOP_ENUMERATE, DESKTOP_READ_OBJECTS, DESKTOPENUMPROC

user32 = windll.user32
kernel32 = windll.kernel32


class DesktopError(RuntimeError):
    def __init__(self, message: str, winerror: int = 0):
        super().__init__(message)
        self.winerror = winerror


def _last_error() -> int:
    return kernel32.GetLastError()


def current_window_station():
    """HWINSTA of the calling thread (WinSta0 for our processes)."""
    return user32.GetProcessWindowStation()


def create_desktop(name: str):
    """CreateDesktopW; returns HDESK (int). Raises DesktopError with GetLastError."""
    user32.CreateDesktopW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
    ]
    user32.CreateDesktopW.restype = wintypes.HANDLE
    h = user32.CreateDesktopW(name, None, None, 0, DESKTOP_ALL, None)
    if not h:
        raise DesktopError(f"CreateDesktopW({name!r}) failed", _last_error())
    return h


def open_desktop(name: str, access: int = DESKTOP_READ_OBJECTS | DESKTOP_ENUMERATE):
    """OpenDesktopW by name. Returns 0 (None-equivalent) when the desktop is gone."""
    user32.OpenDesktopW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    user32.OpenDesktopW.restype = wintypes.HANDLE
    return user32.OpenDesktopW(name, 0, False, access) or 0


def close_desktop(h) -> bool:
    if not h:
        return True
    return bool(user32.CloseDesktop(h))


def set_thread_desktop(h) -> bool:
    """Attach the calling thread to desktop h.

    Hard Win32 constraint: a thread that may change its desktop must not own
    windows and must have no message queue.
    """
    user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
    user32.SetThreadDesktop.restype = wintypes.BOOL
    if not user32.SetThreadDesktop(h):
        err = _last_error()
        raise DesktopError(f"SetThreadDesktop failed (winerror={err})", err)
    return True


def enumerate_desktops():
    """List all desktop names of the process window station (e.g. ['Default', 'dshv-...'])."""
    hsta = current_window_station()
    user32.EnumDesktopsW.argtypes = [
        wintypes.HANDLE,
        DESKTOPENUMPROC,
        wintypes.LPARAM,
    ]
    user32.EnumDesktopsW.restype = wintypes.BOOL
    names: list[str] = []

    def _cb(name, _data) -> bool:
        names.append(name)
        return True

    # keep the callback alive for the duration of the call
    cb = DESKTOPENUMPROC(_cb)
    user32.EnumDesktopsW(hsta, cb, 0)
    return names


def desktop_exists(name: str) -> bool:
    h = open_desktop(name)
    if h:
        close_desktop(h)
        return True
    return False