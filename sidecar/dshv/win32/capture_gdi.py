"""GDI desktop capture on a non-foreground (attached) desktop.

MVP capture path (empirically verified on Win11, M0 — keep these quirks!):
  - the capture thread first calls SetThreadDesktop(target);
  - CreateDC("DISPLAY") on that thread returns a DC for the *target*
    desktop — BitBlt reads its pixels even though the user's screen shows a
    different desktop. No SwitchDesktop anywhere.
  - Empirical quirks baked in below (all reproducible on this machine):
      * freshly created desktop: the display driver attaches lazily — the
        FIRST CreateDC("DISPLAY") fails (error 2, negative sentinel DC that
        is truthy as unsigned) — retry up to 3 times;
      * CreateBitmap(displayDC, ...) fails; CreateBitmap(NULL, ...) works;
      * BitBlt display-DC -> DIB-section fails with ACCESS_DENIED (winerror
        5) for 24/32bpp alike, while display-DC -> DDB (CreateBitmap) works;
      * GetDIBits from a DDB via its memory DC returns only 1 row on this
        machine, while GetDIBits on a DIB section is exact.
    => pipeline: display -> DDB (BitBlt) -> DIB section (BitBlt, mem->mem)
       -> GetDIBits (DIB section) -> GDI+ JPEG/PNG encode of the scanline.
  - pixels are 24bpp BGR top-down; scaling via GDI StretchBlt on DIB
    sections; GDI+ is used only for encoding. No third-party packages.
"""

import os
import threading
from ctypes import (
    byref,
    c_int,
    c_long,
    create_string_buffer,
    create_unicode_buffer,
    c_void_p,
    GetLastError,
    sizeof as ctypes_sizeof,
    windll,
    wintypes,
)

from .defs import (
    BI_RGB,
    BITMAPINFO,
    BITMAPINFOHEADER,
    CAPTUREBLT,
    DIB_RGB_COLORS,
    JPEG_ENCODER,
    JPG_COMPRESSION,
    PNG_ENCODER,
    SRCCOPY,
    EncoderParameter,
    EncoderParameters,
    GdiplusStartupInput,
    GUID,
    make_guid,
)
from . import dpi as _dpi
from .. import encode as _encode_mod

gdi32 = windll.gdi32
gdiplus = windll.gdiplus

# GDI+ pixel format constant (gdiplusinit.h)
PIXELFORMAT_24BPP_RGB = 24 << 12 | 0x0110100

# 64-bit-safe GDI handle APIs (declare once)
gdi32.CreateBitmap.argtypes = [wintypes.HDC, c_int, c_int, c_int, c_int]
gdi32.CreateBitmap.restype = wintypes.HANDLE
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC,
    wintypes.LPVOID,
    wintypes.UINT,
    wintypes.LPVOID,
    wintypes.HANDLE,
    wintypes.UINT,
]
gdi32.CreateDIBSection.restype = wintypes.HANDLE
gdi32.GetDIBits.argtypes = [
    wintypes.HDC,
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.UINT,
    wintypes.LPVOID,
    wintypes.LPVOID,
    wintypes.UINT,
]
gdi32.GetDIBits.restype = wintypes.INT
gdi32.BitBlt.argtypes = [
    wintypes.HDC, c_long, c_long, c_long, c_long,
    wintypes.HDC, c_long, c_long, wintypes.DWORD,
]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.StretchBlt.argtypes = [
    wintypes.HDC, c_long, c_long, c_long, c_long,
    wintypes.HDC, c_long, c_long, c_int, c_int, wintypes.DWORD,
]
gdi32.StretchBlt.restype = wintypes.BOOL
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.CreateDCW.argtypes = [wintypes.LPCWSTR]
gdi32.CreateDCW.restype = wintypes.HDC

_gdiplus_token = c_void_p()
_gdiplus_lock = threading.Lock()
_gdiplus_ready = False


def _ensure_gdiplus() -> None:
    global _gdiplus_ready
    if _gdiplus_ready:
        return
    with _gdiplus_lock:
        if _gdiplus_ready:
            return
        inp = GdiplusStartupInput()
        inp.Size = ctypes_sizeof(GdiplusStartupInput)
        inp.callbacks_or_mode = c_void_p(0)  # GDIPLUS_STARTUP_MODE
        inp.suppressBackgroundThread = 1
        inp.suppressStatusWindow = 1
        hr = gdiplus.GdiplusStartup(byref(_gdiplus_token), byref(inp), None)
        if hr != 0:
            raise RuntimeError(f"GdiplusStartup failed hr={hr}")
        _gdiplus_ready = True


def shutdown_gdiplus() -> None:
    global _gdiplus_ready
    with _gdiplus_lock:
        if _gdiplus_ready:
            gdiplus.GdiplusShutdown(_gdiplus_token)
            _gdiplus_ready = False


def _display_dc(timeout_s: float = 15.0):
    """CreateDC('DISPLAY') with patient retry. Returns a real DC handle.

    On a freshly created desktop the display driver attaches lazily; the
    attach can take far longer than a few milliseconds (empirical: 3
    immediate retries failed while the same call succeeded ~0.5s later on
    other desktops). Poll with short sleeps within a ~15s budget.
    """
    import time as _time

    deadline = _time.monotonic() + timeout_s
    while True:
        cand = gdi32.CreateDCW("DISPLAY")
        if cand and cand < (1 << 63):
            return cand
        if _time.monotonic() >= deadline:
            break
        _time.sleep(0.5)
    raise RuntimeError(
        "CreateDC('DISPLAY') failed after patient retries — is this thread attached to a desktop?"
    )


def _bmi(w: int, h: int) -> BITMAPINFO:
    b = BITMAPINFO()
    b.bmiHeader = BITMAPINFOHEADER(
        biSize=40,
        biWidth=w,
        biHeight=-h,  # negative = top-down
        biPlanes=1,
        biBitCount=24,
        biCompression=BI_RGB,
    )
    return b


class _Grab:
    """Owns the GDI objects of a desktop grab; use as a context manager.

    .hsec/.hsec_dc reference a 24bpp top-down DIB section holding the raw
    desktop rect. .w/.h are the source size.
    """

    def __init__(self, dc_display, x: int, y: int, w: int, h: int):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.bmi = _bmi(w, h)
        self._hddb_dc = gdi32.CreateCompatibleDC(None)
        self._hddb = gdi32.CreateBitmap(None, w, h, 1, 24)
        if not self._hddb:
            self._cleanup()
            raise RuntimeError(f"CreateBitmap failed winerror={GetLastError()}")
        self._prev_dbb = gdi32.SelectObject(self._hddb_dc, self._hddb)
        ok = gdi32.BitBlt(self._hddb_dc, 0, 0, w, h, dc_display, x, y, SRCCOPY | CAPTUREBLT)
        if not ok:
            self._cleanup()
            raise RuntimeError(f"BitBlt(display->DDB) failed winerror={GetLastError()}")

        self.hsec = gdi32.CreateDIBSection(None, byref(self.bmi), 0, None, None, 0)
        if not self.hsec:
            self._cleanup()
            raise RuntimeError(f"CreateDIBSection failed winerror={GetLastError()}")
        self.hsec_dc = gdi32.CreateCompatibleDC(None)
        self._prev_sec = gdi32.SelectObject(self.hsec_dc, self.hsec)
        ok2 = gdi32.BitBlt(self.hsec_dc, 0, 0, w, h, self._hddb_dc, 0, 0, SRCCOPY)
        if not ok2:
            self._cleanup()
            raise RuntimeError(f"BitBlt(DDB->DIBsec) failed winerror={GetLastError()}")

    def read(self) -> "bytes-ish buffer":
        """GetDIBits from the DIB section. Returns a top-down 24bpp buffer
        (rows compacted to width*3, DDB row padding removed)."""
        w, h = self.w, self.h
        pitch = ((w * 3 + 3) // 4) * 4
        raw = create_string_buffer(pitch * h)
        got = gdi32.GetDIBits(self.hsec_dc, self.hsec, 0, h, raw, byref(self.bmi), DIB_RGB_COLORS)
        if got != h:
            raise RuntimeError(f"GetDIBits returned {got} of {h} rows")
        if pitch == w * 3:
            return raw
        compact = create_string_buffer(w * h * 3)
        for row in range(h):
            compact[row * w * 3 : row * w * 3 + w * 3] = raw[row * pitch : row * pitch + w * 3]
        return compact

    def scaled(self, dw: int, dh: int) -> "_Grab":
        """StretchBlt this grab into a smaller DIB-section grab (mem->mem)."""
        other = _Grab.__new__(_Grab)
        other.x, other.y, other.w, other.h = self.x, self.y, dw, dh
        other.bmi = _bmi(dw, dh)
        other.hsec = gdi32.CreateDIBSection(None, byref(other.bmi), 0, None, None, 0)
        if not other.hsec:
            raise RuntimeError(f"CreateDIBSection(scaled) failed winerror={GetLastError()}")
        other.hsec_dc = gdi32.CreateCompatibleDC(None)
        other._prev_sec = gdi32.SelectObject(other.hsec_dc, other.hsec)
        other._hddb_dc = other._hddb = other._prev_dbb = None
        ok = gdi32.StretchBlt(other.hsec_dc, 0, 0, dw, dh, self.hsec_dc, 0, 0, self.w, self.h, SRCCOPY)
        if not ok:
            other._cleanup()
            raise RuntimeError(f"StretchBlt failed winerror={GetLastError()}")
        return other

    def _cleanup(self) -> None:
        for obj, fn in (
            (getattr(self, "hsec", None), gdi32.DeleteObject),
            (getattr(self, "hsec_dc", None), gdi32.DeleteDC),
            (getattr(self, "_hddb", None), gdi32.DeleteObject),
            (getattr(self, "_hddb_dc", None), gdi32.DeleteDC),
        ):
            if obj:
                fn(obj)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._cleanup()
        return False


def _bgr_to_rgb(buf, w: int, h: int) -> bytes:
    """Convert a 24bpp BGR top-down buffer to RGB8 (swap R and B channels)."""
    data = bytes(buf)
    n = w * h
    out = bytearray(n * 3)
    src = 0
    dst = 0
    for _ in range(n):
        out[dst] = data[src + 2]    # R
        out[dst + 1] = data[src + 1]  # G
        out[dst + 2] = data[src]      # B
        src += 3
        dst += 3
    return bytes(out)


def _encode(buf, w: int, h: int, path: str, fmt: str, quality: int) -> None:
    """Pure-Python-encode a 24bpp BGR top-down buffer to jpeg/png at `path`.

    Uses dshv.encode.save_image (zlib PNG / hand-rolled baseline JPEG) instead
    of GDI+, per the user's requirement to avoid GDI+ dependencies.
    """
    rgb = _bgr_to_rgb(buf, w, h)
    _encode_mod.save_image(path, rgb, w, h, fmt, quality)


def capture_zoom_desktop(out_path: str, region, scale: float = 2.0,
                         fmt: str = "png", quality: int = 80) -> dict:
    """Zoom region then StretchBlt upscale (1.0-4.0)."""
    if fmt not in ("jpeg", "png"):
        raise ValueError("fmt must be jpeg or png")
    scale = max(1.0, min(4.0, float(scale)))
    x, y, w, h = [int(v) for v in region]
    vx, vy, vw, vh = _dpi.virtual_screen_rect()
    x = max(x, vx)
    y = max(y, vy)
    x2 = min(x + w, vx + vw)
    y2 = min(y + h, vy + vh)
    w = x2 - x
    h = y2 - y
    if w <= 0 or h <= 0:
        raise ValueError("region outside virtual screen")
    dw = max(1, int(round(w * scale)))
    dh = max(1, int(round(h * scale)))
    dc = _display_dc()
    try:
        grab = _Grab(dc, x, y, w, h)
        try:
            up = grab.scaled(dw, dh)
            try:
                buf = up.read()
            finally:
                up._cleanup()
            dw, dh = up.w, up.h
        finally:
            grab._cleanup()
    finally:
        gdi32.DeleteDC(dc)
    _encode(buf, dw, dh, out_path, fmt, quality)
    return {
        "imagePath": os.path.abspath(out_path),
        "width": dw,
        "height": dh,
        "sourceRect": [x, y, w, h],
        "scale": round(scale, 3),
    }


def capture_desktop(
    out_path: str,
    max_long_edge: int = 1280,
    fmt: str = "jpeg",
    quality: int = 80,
    region=None,
) -> dict:
    """Capture the desktop the *calling thread* is attached to.

    `region` is a desktop-physical-px rect (x, y, w, h); None = full virtual
    screen. The calling thread MUST be attached to the target desktop and the
    process DPI-aware. Returns {imagePath,width,height,sourceWidth,
    sourceHeight,scale,origin}.
    """
    if fmt not in ("jpeg", "png"):
        raise ValueError("fmt must be 'jpeg' or 'png'")
    vx, vy, vw, vh = _dpi.virtual_screen_rect()
    if region is None:
        x, y, w, h = vx, vy, vw, vh
    else:
        x, y, rw, rh = region
        x = max(x, vx)
        y = max(y, vy)
        x2 = min(x + rw, vx + vw)
        y2 = min(y + rh, vy + vh)
        w, h = x2 - x, y2 - y
        if w <= 0 or h <= 0:
            raise ValueError(f"region {region} outside virtual screen ({vx},{vy},{vw},{vh})")

    dc = _display_dc()
    try:
        grab = _Grab(dc, x, y, w, h)
        try:
            long_edge = max(w, h)
            if long_edge > max_long_edge:
                scale = max_long_edge / long_edge
                dw = max(1, int(round(w * scale)))
                dh = max(1, int(round(h * scale)))
                small = grab.scaled(dw, dh)
                try:
                    buf = small.read()
                finally:
                    small._cleanup()
                dw, dh = small.w, small.h
            else:
                scale = 1.0
                dw, dh = w, h
                buf = grab.read()
        finally:
            grab._cleanup()
    finally:
        gdi32.DeleteDC(dc)

    _encode(buf, dw, dh, out_path, fmt, quality)
    return {
        "imagePath": os.path.abspath(out_path),
        "width": dw,
        "height": dh,
        "sourceWidth": w,
        "sourceHeight": h,
        "scale": round(scale, 6),
        "origin": [x, y],
    }