"""DPI awareness + virtual-screen metrics.

Call :func:`enable_dpi_awareness` once at process start (main thread) before
creating any DC or reading metrics. Internal coordinates in this package are
always physical pixels.
"""

from ctypes import windll

from .defs import (
    PER_MONITOR_AWARE_V2_HANDLE,
    SM_CXSCREEN,
    SM_CXVIRTUALSCREEN,
    SM_CYSCREEN,
    SM_CYVIRTUALSCREEN,
    SM_XVIRTUALSCREEN,
    SM_YVIRTUALSCREEN,
)

user32 = windll.user32


def enable_dpi_awareness() -> None:
    """Set PER_MONITOR_AWARE_V2 (Win10 1607+), falling back to system aware."""
    if hasattr(user32, "SetProcessDpiAwarenessContext"):
        ok = user32.SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2_HANDLE)
        if ok:
            return
    try:
        shcore = windll.shcore
        if hasattr(shcore, "SetProcessDpiAwareness"):
            # 2 == PER_MONITOR_DPI_AWARE
            if shcore.SetProcessDpiAwareness(2) == 0:
                return
    except OSError:
        pass
    user32.SetProcessDPIAware()


def virtual_screen_rect():
    """Return (x, y, w, h) of the virtual screen in physical pixels."""
    x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return x, y, w, h


def primary_screen_size():
    return (
        user32.GetSystemMetrics(SM_CXSCREEN),
        user32.GetSystemMetrics(SM_CYSCREEN),
    )