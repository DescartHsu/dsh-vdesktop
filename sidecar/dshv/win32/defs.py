# Win32 constants, structs, and helper types for ctypes.
#
# Pure-ctypes layer: no third-party dependencies. All structs match the
# 64-bit (x64) Windows ABI. Keep this file the single source of truth for
# FFI shapes; other modules import from here.

from ctypes import (
    Structure,
    Union,
    WINFUNCTYPE,
    c_byte,
    c_int,
    c_long,
    c_longlong,
    c_uint,
    c_ulong,
    c_ushort,
    c_void_p,
    wintypes,
)

# ---------------------------------------------------------------- desktop ---

DESKTOP_READ_OBJECTS = 0x00000001
DESKTOP_CREATE_WINDOW = 0x00000002
DESKTOP_READ_WINDOW = 0x00000008
DESKTOP_CREATE_MENU = 0x00000010
DESKTOP_ENUMERATE = 0x00000010
DESKTOP_EXECUTE_APP = 0x00000020
DESKTOP_WRITE_WINDOW = 0x00000040
DESKTOP_ALL = 0x000001FF

DESKTOP_READ_ACCESS = (
    DESKTOP_READ_OBJECTS | DESKTOP_READ_WINDOW | DESKTOP_ENUMERATE
)

# ---------------------------------------------------------------- process ---

PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000

CREATE_UNICODE_ENVIRONMENT = 0x00000400
DETACHED_PROCESS = 0x00000008
CREATE_NEW_CONSOLE = 0x00000010

# ----------------------------------------------------------------- gdi/dx ---

SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
BI_RGB = 0
DIB_RGB_COLORS = 0
UNIT_PIXEL = 2

# GDI+ encoder CLSIDs
JPEG_ENCODER = (0x5484962F, 0x5DF7, 0x11D4, (0xB7, 0x7F, 0x00, 0x00, 0xF8, 0x03, 0xDF, 0x73))
PNG_ENCODER = (0x54507378, 0x5DF7, 0x11D4, (0xB7, 0x7F, 0x00, 0x00, 0xF8, 0x03, 0xDF, 0x73))
JPG_COMPRESSION = (0x27E812C7, 0x797A, 0x4944, (0x8B, 0x6D, 0x66, 0x80, 0x22, 0xD8, 0x57, 0x6A))

# ----------------------------------------------------------------- input ---

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
INPUT_HARDWARE = 2

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_ABSOLUTE_V2 = 0x4000
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

# Virtual key codes (full table in _VK_TABLE below)
VK_TABLE = {
    "lbutton": 0x01,
    "rbutton": 0x02,
    "back": 0x03,
    "cancel": 0x03,
    "mbutton": 0x04,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "menu": 0x12,
    "alt": 0x12,
    "pause": 0x13,
    "altgr": 0x14,
    "space": 0x20,
    "prior": 0x21,
    "pageup": 0x21,
    "next": 0x22,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "select": 0x29,
    "print": 0x2A,
    "execute": 0x2B,
    "printscreen": 0x2C,
    "snap": 0x2C,
    "insert": 0x2D,
    "delete": 0x2E,
    "help": 0x2F,
    "numlock": 0x90,
    "scroll": 0x91,
    "browser_back": 0xA6,
    "browser_forward": 0xA7,
    "browser_refresh": 0xA7,
    "volume_mute": 0xAD,
    "volume_down": 0xAE,
    "volume_up": 0xAF,
    "apps": 0x5D,
}
# digits
for _i in range(10):
    VK_TABLE[str(_i)] = 0x30 + _i
# letters
for _i in range(26):
    VK_TABLE[chr(ord("a") + _i)] = 0x40 + _i
# F1..F24
for _i in range(1, 25):
    VK_TABLE[f"f{_i}"] = 0x70 + (_i - 1)
# named single-char keys
VK_TABLE["backspace"] = 0x08
VK_TABLE["tab"] = 0x09
VK_TABLE["enter"] = 0x0D
VK_TABLE["return"] = 0x0D
VK_TABLE["escape"] = 0x1B
VK_TABLE["esc"] = 0x1B
VK_TABLE["capslock"] = 0x14
VK_TABLE["win"] = 0x5B
VK_TABLE["lwin"] = 0x5B
VK_TABLE["rwin"] = 0x5C
VK_TABLE["deadchar"] = 0xF6

# DPI awareness constants (SetProcessDpiAwarenessContext)
DPI_AWARENESS_UNAWARE = 0
DPI_AWARENESS_SYSTEM_AWARE = 1
DPI_AWARENESS_PER_MONITOR_AWARE = 2
DPI_AWARENESS_PER_MONITOR_AWARE_V2 = 3
DPI_AWARENESS_IGNORABLE = 4
PER_MONITOR_AWARE_V2_HANDLE = -4  # DPI_AWARENESS_CONTEXT enum value
SYSTEM_AWARE_HANDLE = -3
UNAWARE_HANDLE = -1

# window station / metrics
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CXSCREEN = 0
SM_CYSCREEN = 1

WINSTATION_NAME_DEFAULT = "WinSta0"
DESKTOP_DEFAULT_NAME = "Default"

# ----------------------------------------------------------------- structs --


class STARTUPINFOW(Structure):
    _fields_ = [
        ("cb", c_uint),
        ("lpReserved", wintypes.LPVOID),
        ("lpDesktop", wintypes.LPCWSTR),
        ("dwFlags", c_uint),
        ("wShowWindow", c_ushort),
        ("x", c_ushort),
        ("y", c_ushort),
        ("cx", c_ushort),
        ("cy", c_ushort),
        ("wFillForeground", c_uint),
        ("wFillBackground", c_uint),
        ("dwCursor", c_uint),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", c_ulong),
        ("dwThreadId", c_ulong),
    ]


class KEYBDINPUT(Structure):
    _fields_ = [
        ("wVk", c_ushort),
        ("wScan", c_ushort),
        ("dwFlags", c_ulong),
        ("time", c_ulong),
        ("dwExtraInfo", c_longlong),
    ]


class MOUSEINPUT(Structure):
    _fields_ = [
        ("dx", c_long),
        ("dy", c_long),
        ("mouseData", c_ulong),
        ("dwFlags", c_ulong),
        ("time", c_ulong),
        ("dwExtraInfo", c_longlong),
    ]


class HARDWAREINPUT(Structure):
    _fields_ = [
        ("uMsg", c_ulong),
        ("wParamL", c_ushort),
        ("wParamH", c_ushort),
    ]


class _INPUT_UNION(Union):
    _fields_ = [
        ("ki", KEYBDINPUT),
        ("mi", MOUSEINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(Structure):
    _fields_ = [
        ("type", c_ulong),
        ("u", _INPUT_UNION),
    ]


class BITMAPINFOHEADER(Structure):
    _fields_ = [
        ("biSize", c_uint),
        ("biWidth", c_long),
        ("biHeight", c_long),
        ("biPlanes", c_ushort),
        ("biBitCount", c_ushort),
        ("biCompression", c_uint),
        ("biSizeImage", c_uint),
        ("biXPelsPerMeter", c_long),
        ("biYPelsPerMeter", c_long),
        ("biClrUsed", c_uint),
        ("biClrImportant", c_uint),
    ]


class RGBQUAD(Structure):
    _fields_ = [
        ("rgbBlue", c_byte),
        ("rgbGreen", c_byte),
        ("rgbRed", c_byte),
        ("rgbReserved", c_byte),
    ]


class BITMAPINFO(Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", RGBQUAD * 256),
    ]


class POINT(Structure):
    _fields_ = [
        ("x", c_long),
        ("y", c_long),
    ]


class RECT(Structure):
    _fields_ = [
        ("left", c_long),
        ("top", c_long),
        ("right", c_long),
        ("bottom", c_long),
    ]


# GDI+ ---------------------------------------------------------------


class GdiplusStartupInput(Structure):
    # First data field after Size is a union { void* DebugCallbacks; int mode; }
    _fields_ = [
        ("Size", c_ulong),
        ("callbacks_or_mode", c_void_p),
        ("suppressBackgroundThread", c_int),
        ("suppressStatusWindow", c_int),
    ]


class GUID(Structure):
    """COM GUID layout (wintypes.GUID is absent on some Python builds)."""

    _fields_ = [
        ("data1", c_ulong),
        ("data2", c_ushort),
        ("data3", c_ushort),
        ("data4", c_byte * 8),
    ]


def make_guid(t):
    g = GUID()
    g.data1 = t[0]
    g.data2 = t[1]
    g.data3 = t[2]
    g.data4 = (c_byte * 8)(*t[3])
    return g


class EncoderParameter(Structure):
    _fields_ = [
        ("Guid", GUID),
        ("Size", c_uint),
        ("Value", c_void_p),
        ("NumberOfValues", c_uint),
    ]


class EncoderParameters(Structure):
    _fields_ = [
        ("Count", c_uint),
        ("NumberOfTypes", c_uint),
        ("Parameter", EncoderParameter),
    ]


# window enumeration callback: BOOL CALLBACK EnumWindowsProc(HWND, LPARAM)
WNDENUMPROC = WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
# desktop enumeration: user32's real export passes 2 args (name, data) —
# the documented HWINSTA first param is NOT passed (empirically verified on Win11).
DESKTOPENUMPROC = WINFUNCTYPE(wintypes.BOOL, wintypes.LPWSTR, wintypes.LPARAM)