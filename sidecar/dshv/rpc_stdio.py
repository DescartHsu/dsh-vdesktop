"""stdio 行 JSON-RPC 服务端。

stdin 逐行读 request {id, method, params}，stdout 逐行回 {id, result} 或
{id, error:{code, message}}。分帧与 M0 CLI demo 命令同语义，方法名严格对齐 TS 侧：
 desktop.create / desktop.close / desktop.list / desktop.sweep
 capture.shot
 input.type / input.click / input.read_text
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import time
import traceback

from dshv.win32 import desktop as win_desktop
from dshv.win32 import process as win_process
from dshv.win32 import input_chain as ic
from dshv.win32 import capture_dual
from dshv.win32 import capture_dual as cap  # 大脑 0aa2669e6cd7 替换块 2A
from dshv.win32 import dpi
from dshv.win32.defs import DESKTOP_ALL, RECT
from ctypes import byref as _byref

# 错误码：优先复用 rpc.py 的常量，缺失则回退 JSON-RPC 标准码。
try:
    from dshv import rpc as _rpc_mod
    E_PARSE = getattr(_rpc_mod, "E_PARSE", -32700)
    E_INVALID_REQUEST = getattr(_rpc_mod, "E_INVALID_REQUEST", -32600)
    E_METHOD_NOT_FOUND = getattr(_rpc_mod, "E_METHOD_NOT_FOUND", -32601)
    E_INVALID_PARAMS = getattr(_rpc_mod, "E_INVALID_PARAMS", -32602)
    E_INTERNAL = getattr(_rpc_mod, "E_INTERNAL", -32603)
except Exception:
    E_PARSE = -32700
    E_INVALID_REQUEST = -32600
    E_METHOD_NOT_FOUND = -32601
    E_INVALID_PARAMS = -32602
    E_INTERNAL = -32603

_USER32 = ctypes.windll.user32

# name -> hDesktop 句柄表（进程内）
_SESSIONS: dict = {}

# ---------------------------------------------------------------- shell 守卫
# 大脑 0205ec96b216：withShell（vdesk 拉起自己的 explorer：壁纸/任务栏/
# 开始菜单，可点但表面渲染受环境限制）+ close 双向 shell 保护。
# 主桌面 shell PID 基线必须在主循环前缓存（首次 RPC 后主线程可能已 attach
# 到 vdesk，EnumWindows 枚举到的就变成 vdesk 了；禁止硬编码 PID）。
_SHELL_CLASSES_ALL = (
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
)
_PRIMARY_SHELL_PIDS: set | None = None

# 修复 A 约束 1（大脑 bdad6e77a8ce）：Default 桌面句柄模块级缓存，
# 避免每次 close 都 open_desktop 造成句柄泄漏。
_DEFAULT_DESKTOP_HANDLE = None


def _ensure_default_desktop_handle():
    global _DEFAULT_DESKTOP_HANDLE
    if _DEFAULT_DESKTOP_HANDLE is None:
        try:
            _DEFAULT_DESKTOP_HANDLE = win_desktop.open_desktop("Default")
        except Exception as e:  # noqa: BLE001
            _dbg("open Default desktop failed: %r" % e)
    return _DEFAULT_DESKTOP_HANDLE


def _capture_primary_shell_pids() -> set:
    """抓主桌面（调用线程所在桌面）shell 类窗 PID 全集，缓存。

    必须在 serve() 主循环前调用（进程启动时主线程仍在主桌面）。
    值经 create/close 响应回传（sidecar stdout 是 RPC 通道，不打日志防污染）。
    """
    global _PRIMARY_SHELL_PIDS
    if _PRIMARY_SHELL_PIDS is not None:
        return _PRIMARY_SHELL_PIDS
    from dshv.win32.defs import WNDENUMPROC
    pids: set = set()
    try:
        def _cb(hwnd, _l) -> bool:
            try:
                cls = _class_name(hwnd)
                if cls in _SHELL_CLASSES_ALL:
                    pid = ctypes.wintypes.DWORD(0)
                    _USER32.GetWindowThreadProcessId(hwnd, _byref(pid))
                    if pid.value:
                        pids.add(int(pid.value))
            except Exception:
                pass
            return True
        cb = WNDENUMPROC(_cb)
        _USER32.EnumWindows(cb, 0)
    except Exception:
        pass
    _PRIMARY_SHELL_PIDS = pids
    return pids


def _dbg(msg: str) -> None:
    if os.environ.get("DSHV_DEBUG"):
        sys.stderr.write("[dbg] " + msg + "\n")
        sys.stderr.flush()


def _shell_pids_on_desktop(hdesk) -> set:
    """枚举指定桌面上的 shell 类窗 PID（vdesk 自己 explorer 的 PID 集合）。"""
    from dshv.win32.defs import WNDENUMPROC
    pids: set = set()
    try:
        def _cb(hwnd, _l) -> bool:
            try:
                cls = _class_name(hwnd)
                if cls in _SHELL_CLASSES_ALL:
                    pid = ctypes.wintypes.DWORD(0)
                    _USER32.GetWindowThreadProcessId(hwnd, _byref(pid))
                    if pid.value:
                        pids.add(int(pid.value))
            except Exception:
                pass
            return True
        cb = WNDENUMPROC(_cb)
        rc = _USER32.EnumDesktopWindows(hdesk, cb, 0)
        _dbg("_shell_pids_on_desktop hdesk=%s rc=%s pids=%s thread_desktop=%s" % (
            hdesk, rc, sorted(pids), _USER32.GetThreadDesktop(0)))
    except Exception as e:  # noqa: BLE001
        _dbg("_shell_pids_on_desktop exc=%r" % e)
    return pids


class _BadParams(Exception):
    pass


def _class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(64)
    _USER32.GetClassNameW(hwnd, buf, 64)
    return buf.value


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def err(id, code, message, data=None):
    e = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    _send({"id": id, "error": e})


def ok(id, result):
    _send({"id": id, "result": result})


def _require(params, key):
    if not isinstance(params, dict) or key not in params:
        raise _BadParams("missing required param: " + str(key))
    return params[key]


def _open_or_get(name: str) -> int:
    h = _SESSIONS.get(name)
    if h:
        return h
    h = win_desktop.open_desktop(name, DESKTOP_ALL)
    _SESSIONS[name] = h
    return h


_TARGET_CLASS_PRIORITY = ("Notepad",)


def _pick_top_and_target(hdesk):
    """找目标顶层窗 + 其第一个可见 Edit 子窗（若有）。

    新 Win11 notepad 启动时会先冒一个 #32770 小窗（349×195），
    旧逻辑"第一个非 shell 顶层窗"会误选它、target 永远空。
    选窗规则：
    1. 优先 class 命中 _TARGET_CLASS_PRIORITY；
    2. 回退：可见且 w>=300 且 h>=300 的第一个非 shell 顶层窗。
    """
    candidates = []
    for h in win_process.enumerate_desktop_windows(hdesk):
        info = win_process.window_info(h)
        cls = info.get("class", "")
        if cls in ("Progman", "WorkerW", "Shell_TrayWnd"):
            continue
        candidates.append((h, info, cls))

    top = None
    for h, info, cls in candidates:
        if cls in _TARGET_CLASS_PRIORITY:
            top = h
            break

    if top is None:
        for h, info, cls in candidates:
            r = info.get("rect") or [0, 0, 0, 0]
            w = (r[2] or 0) - (r[0] or 0)
            hh = (r[3] or 0) - (r[1] or 0)
            if w >= 300 and hh >= 300:
                top = h
                break

    target = None
    if top:
        for c in ic.find_child_windows(top):
            if c["class"] == "Edit" and c.get("visible"):
                target = c["hwnd"]
                break
    return top, target


def _wait_for_target(hdesk, timeout_s=5.0):
    """轮询等待目标桌面顶层窗及其 Edit 子窗就绪（裁决 d1653f604606：
    create 后新 notepad 子窗口树未就绪的竞态，handler 不得直接用 _pick_top_and_target）。
    """
    t0 = time.time()
    top, target = _pick_top_and_target(hdesk)
    while (top is None or target is None) and time.time() - t0 < timeout_s:
        time.sleep(0.1)
        top, target = _pick_top_and_target(hdesk)
    return top, target


def _pick_top_and_target_fast(hdesk):
    """input_event 专用快速选窗（冻结桌面安全，M2 窗口模式最小版）。

    window_info 的 GetWindowTextW 与 find_child_windows 的子窗 GetWindowTextW/
    GetWindowRect 都会对冻结桌面无响应的窗线程发消息而阻塞（py-spy 实证：
    挂死在 process.py:145 GetWindowTextW）。本路径只用
    GetClassNameW/IsWindowVisible/GetWindowThreadProcessId（内核态）。
    代价：无 title/rect，回退分支（原 w/h>=300 过滤）退化为"首个可见
    非 shell 顶层窗"。
    """
    top = None
    first_visible = None
    for h in win_process.enumerate_desktop_windows(hdesk):
        try:
            info = win_process.window_info_fast(h)
        except Exception:
            continue
        if not info.get("visible"):
            continue
        cls = info.get("class", "")
        if not cls or cls in win_process._SHELL_CLASSES:
            continue
        # M5b：跳自身进程窗口（心跳锚窗）与 Static 类（任一 sidecar 的锚窗）——
        # 锚窗 Z 序最前，不跳过会吞掉全部点击/输入（用户实测"点击没效果"根因）
        if info.get("pid") == os.getpid() or cls == "Static":
            continue
        if first_visible is None:
            first_visible = h
        if cls in _TARGET_CLASS_PRIORITY:
            top = h
            break
    if top is None:
        top = first_visible
    target = ic.find_edit_child_fast(top) if top else None
    return top, target


def _wait_for_target_fast(hdesk, timeout_s=5.0):
    """快路径版 create 竞态等待（同 _wait_for_target，但冻结桌面安全）。

    M6 #2：语义收紧——只等 top 到超时；top 就绪后 target（Edit 子窗）只给
    200ms 宽限（notepad 子窗竞态），仍无则按 top 读。修复 chrome 等无 Edit
    子窗的窗口被空等满 5s → input timeout → 面板输入框不清空的连锁问题。
    """
    t0 = time.time()
    top, target = _pick_top_and_target_fast(hdesk)
    while top is None and time.time() - t0 < timeout_s:
        time.sleep(0.1)
        top, target = _pick_top_and_target_fast(hdesk)
    if top is not None and target is None:
        t1 = time.time()
        while target is None and time.time() - t1 < 0.2:
            time.sleep(0.05)
            top, target = _pick_top_and_target_fast(hdesk)
    return top, target


def _hit_test_window(hdesk, x, y):
    """M5b：坐标命中测试选窗（Z 序从前到后，首个窗口矩形包含该点的窗）。

    只用内核态调用（GetClassNameW/IsWindowVisible/GetWindowThreadProcessId/
    GetWindowRect），冻结桌面安全；GetWindowRect 是 M4 _exec_event_click
    in_target 判定已用的同一调用。跳过自身进程窗口（心跳锚窗）与 Static 类
    （其它 sidecar 的锚窗）；先过非 shell 窗，再过 shell 窗（任务栏/桌面
    背景）；全部未命中返回 None（调用方回退 _pick_top_and_target_fast）。
    """
    me = os.getpid()
    non_shell = []
    shell = []
    for h in win_process.enumerate_desktop_windows(hdesk):
        try:
            info = win_process.window_info_fast(h)
        except Exception:
            continue
        if not info.get("visible"):
            continue
        cls = info.get("class", "")
        if not cls or info.get("pid") == me or cls == "Static":
            continue
        try:
            r = RECT()
            _USER32.GetWindowRect(h, _byref(r))
        except Exception:
            continue
        (shell if cls in win_process._SHELL_CLASSES else non_shell).append(
            (h, r.left, r.top, r.right, r.bottom))
    for bucket in (non_shell, shell):
        for h, l, t, rt, b in bucket:
            if l <= x < rt and t <= y < b:
                return h
    return None


def _edit_child_at(hwnd_top, x, y):
    """M5b：首个矩形包含 (x,y) 的可见 Edit 子窗（内核态）；无则 None。

    精确 target：标题栏点击 → None（点击落顶层窗，双击最大化等行为仍可用）；
    文本区点击 → Edit 子窗（notepad 输入落点）。只用 EnumChildWindows/
    GetClassNameW/IsWindowVisible/GetWindowRect，冻结桌面安全。
    """
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(hwnd, _lparam):
        try:
            buf = ctypes.create_unicode_buffer(64)
            _USER32.GetClassNameW(hwnd, buf, 64)
            if buf.value != "Edit":
                return 1
            if not _USER32.IsWindowVisible(hwnd):
                return 1
            r = RECT()
            _USER32.GetWindowRect(hwnd, _byref(r))
            if r.left <= x < r.right and r.top <= y < r.bottom:
                out.append(int(hwnd))
                return 0
        except Exception:
            pass
        return 1

    _USER32.EnumChildWindows(int(hwnd_top), _cb, 0)
    return out[0] if out else None


def _child_at(hwnd_top, x, y):
    """M7 #3（用户裁决）：首个（最深层）矩形包含 (x,y) 的可见子窗。

    经典任务栏（Shell_TrayWnd）的按钮活在子窗里（MSTaskListWClass 按钮区 /
    Start 按钮子窗），PostMessage 落顶层窗不转发给子窗 → 任务栏点击无反应。
    对 shell 窗启用子窗命中后，点击发给该子窗（click_chain 内 ScreenToClient
    折子窗客户区）。只用 EnumChildWindows/IsWindowVisible/GetWindowRect，
    冻结桌面安全。
    """
    best = {"hwnd": None}

    @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(hwnd, _lparam):
        try:
            if not _USER32.IsWindowVisible(hwnd):
                return 1
            r = RECT()
            _USER32.GetWindowRect(hwnd, _byref(r))
            if r.left <= x < r.right and r.top <= y < r.bottom:
                deeper = _child_at(int(hwnd), x, y)
                best["hwnd"] = deeper if deeper else int(hwnd)
        except Exception:
            pass
        return 1

    _USER32.EnumChildWindows(int(hwnd_top), _cb, 0)
    return best["hwnd"]


def _click_target_for(top, x, y):
    """点击 target 选择：Edit 子窗（文本区）优先；无则对任务栏做子窗命中；
    其余窗口保持落顶层（chrome 等已验证行为）。"""
    target = _edit_child_at(top, x, y)
    if target is None:
        try:
            if _class_name(top) == "Shell_TrayWnd":
                target = _child_at(top, x, y)
        except Exception:
            target = None
    return target


# ---------------- M7 #4：任务栏点击 → 窗口管理等价动作 ----------------
# 实测（test_taskbar_click_variants*.py，8 种方式）：任务栏按钮是自定义子窗，
# 外部 post/send/WM_POINTER/keybd_event 物键状态均无法触发其按钮状态机
# （同步 SendMessage 也被 explorer 处理但无效果）。vdesk 线程上 COM 初始化
# 也失败（MTA access denied / STA invalid parameter），ITaskbarList4 枚举
# 路线暂不可用。故任务栏区域点击直接做等价窗口管理动作（真实按钮的语义）。

_SW_SHOW, _SW_MINIMIZE, _SW_RESTORE = 5, 6, 9


_TASKBAR_BTN_LEFT = 48
_TASKBAR_BTN_WIDTH = 48

# M9: 任务栏按钮序列表（实景 7 按钮：开始|WPS|VSCode|Chrome|Explorer|圆形|Chrome）
_TASKBAR_BUTTONS = (
    {"key": "wps",
     "exe": r"D:\Tools\WPS Office\ksolaunch.exe",
     "args": "/prometheus /fromksolaunch /from=taskbar",
     "exe_names": ("ksolaunch.exe", "wps.exe", "wpscloudsvr.exe", "et.exe", "wpp.exe")},
    {"key": "vscode",
     "exe": r"D:\Tools\Microsoft VS Code\Code.exe",
     "args": "", "exe_names": ("code.exe",)},
    {"key": "chrome",
     "exe": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
     "args": "", "exe_names": ("chrome.exe",)},
    {"key": "explorer",
     "exe": r"C:\Windows\explorer.exe",
     "args": "", "exe_names": ("explorer.exe",), "classes": ("CabinetWClass",)},
    {"key": "unknown", "exe": None, "args": "", "exe_names": ()},
    {"key": "chrome",
     "exe": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
     "args": "", "exe_names": ("chrome.exe",)},
)

_EXE_NAME_CACHE: dict = {}


def _exe_name_of_pid(pid):
    """pid → exe 文件名（小写，无路径）。失败返回 ""。带进程内缓存。"""
    try:
        pid = int(pid)
    except Exception:
        return ""
    if not pid:
        return ""
    if pid in _EXE_NAME_CACHE:
        return _EXE_NAME_CACHE[pid]
    name = ""
    try:
        _k32 = ctypes.windll.kernel32
        h = _k32.OpenProcess(0x1000, False, pid)
        if h:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = ctypes.c_ulong(1024)
                if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    name = os.path.basename(buf.value).lower()
            finally:
                _k32.CloseHandle(h)
    except Exception:
        name = ""
    _EXE_NAME_CACHE[pid] = name
    return name


def _taskbar_button_index_action(hdesk, x, y, desktop_name):
    """M9: x 落在任务栏按钮区 → 按 (x-48)//48 换算按钮序号 → 激活/启动对应应用。

    返回 dict 或 None（x 在开始键左侧 / 序号越界 → 调用方回退旧逻辑）。
    """
    if x < _TASKBAR_BTN_LEFT:
        return None
    idx = int((x - _TASKBAR_BTN_LEFT) // _TASKBAR_BTN_WIDTH)
    if idx < 0 or idx >= len(_TASKBAR_BUTTONS):
        return None
    btn = _TASKBAR_BUTTONS[idx]
    exe_names = btn.get("exe_names") or ()
    classes = btn.get("classes") or ()
    match = []
    for item in _list_app_windows(hdesk):
        h, cls, _title, iconic = item[0], item[1], item[2], item[3]
        pid = 0
        try:
            _p = ctypes.c_ulong(0)
            _USER32.GetWindowThreadProcessId(int(h), _byref(_p))
            pid = _p.value
        except Exception:
            pid = 0
        exe = _exe_name_of_pid(pid)
        ok = False
        if classes and cls in classes:
            ok = True
        elif exe_names and exe in exe_names:
            ok = True
        if ok:
            match.append((h, cls, _title, iconic))
    if match:
        _ic = [a for a in match if a[3]]
        pick = _ic[0] if _ic else match[0]
        try:
            for a in match:
                if a[3]:
                    _USER32.ShowWindow(a[0], _SW_RESTORE)
            _USER32.ShowWindow(pick[0], _SW_RESTORE)
            _USER32.SetForegroundWindow(pick[0])
        except Exception as e:
            return {"ok": True, "method": "taskbar", "action": "activate_by_index",
                    "detail": "按钮#%d(%s) 激活失败: %r" % (idx + 1, btn["key"], e)}
        return {"ok": True, "method": "taskbar", "action": "activate_by_index",
                "detail": "按钮#%d → %s（匹配 %d 窗）" % (idx + 1, btn["key"], len(match))}
    if not btn.get("exe"):
        return {"ok": True, "method": "taskbar", "action": "noop_by_index",
                "detail": "按钮#%d 无运行中窗且无固定目标" % (idx + 1)}
    if not desktop_name:
        return {"ok": True, "method": "taskbar", "action": "launch_by_index",
                "detail": "按钮#%d 需启动 %s（缺 desktop 名）" % (idx + 1, btn["exe"])}
    try:
        win_process.create_process_on_desktop(
            desktop_name, btn["exe"], args=btn.get("args") or "",
            wait_window_ms=0)
        return {"ok": True, "method": "taskbar", "action": "launch_by_index",
                "detail": "按钮#%d 已启动 %s" % (idx + 1, btn["exe"])}
    except Exception as e:
        return {"ok": True, "method": "taskbar", "action": "launch_by_index",
                "detail": "按钮#%d 启动失败 %s: %r" % (idx + 1, btn["exe"], e)}


_TRAY_CHILD_CLASSES = ("Start", "MSTaskListWClass", "TrayNotifyWnd",
                       "TrayShowDesktopButtonWClass", "TrayClockWClass")


def _tray_child_rects(tray):
    """任务栏子窗屏幕矩形，按类名索引（仅可见子窗）。"""
    out = {}

    @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(hwnd, _lp):
        try:
            buf = ctypes.create_unicode_buffer(64)
            _USER32.GetClassNameW(hwnd, buf, 64)
            if buf.value in _TRAY_CHILD_CLASSES and \
                    _USER32.IsWindowVisible(hwnd):
                r = RECT()
                _USER32.GetWindowRect(hwnd, _byref(r))
                out[buf.value] = (r.left, r.top, r.right, r.bottom)
        except Exception:
            pass
        return 1

    _USER32.EnumChildWindows(int(tray), _cb, 0)
    return out


def _list_app_windows(hdesk, exclude_pid=None, allow_classes_on_exclude=()):
    """vdesk 顶层应用窗（含最小化）：可见或图标态、非 shell 类、非 Static
    锚窗、非自身进程、宽高>4px。枚举顺序 = Z 序前到后。
    exclude_pid：排除指定进程（vdesk shell 进程——其自身窗口如"此电脑"
    不占应用按钮位，v1 裁决）。
    返回 [(hwnd, class, title, iconic), ...]。"""
    me = os.getpid()
    apps = []
    for h in win_process.enumerate_desktop_windows(hdesk):
        try:
            info = win_process.window_info_fast(h)
        except Exception:
            continue
        pid = info.get("pid")
        cls = info.get("class", "")
        if not cls or pid == me or cls == "Static" \
                or cls in win_process._SHELL_CLASSES:
            continue
        # m9d: shell 进程窗白名单（如 CabinetWClass 资源管理器文件夹窗）
        if exclude_pid and pid == exclude_pid and \
                cls not in (allow_classes_on_exclude or ()):
            continue
        try:
            r = RECT()
            _USER32.GetWindowRect(h, _byref(r))
        except Exception:
            continue
        if (r.right - r.left) <= 4 or (r.bottom - r.top) <= 4:
            continue
        try:
            iconic = bool(_USER32.IsIconic(h))
        except Exception:
            iconic = False
        if not (info.get("visible") or iconic):
            continue
        apps.append((h, cls, info.get("title", ""), iconic))
    return apps


def _pinned_taskbar_apps():
    """HKCU Taskband Buttons（固定项，值=exe 名或完整路径）。"""
    try:
        import winreg
        pinned = []
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion"
                            r"\Explorer\Taskband\Buttons") as k:
            n = winreg.QueryInfoKey(k)[0]
            for i in range(n):
                _name, value, _t = winreg.EnumValue(k, i)
                pinned.append(str(value))
        return pinned
    except Exception:
        return []


def _taskbar_click_action(hdesk, x, y, tray, desktop_name=""):
    """M7 #4（用户裁决）：任务栏区域点击 → 等价窗口管理动作。

    返回 dict {ok, method:"taskbar", action, detail} 或 None（未识别区域，
    调用方回退 click_chain 保持旧行为）。
    """
    try:
        rects = _tray_child_rects(tray)
        shell_pid = 0
        try:
            _pid = ctypes.wintypes.DWORD(0)
            _USER32.GetWindowThreadProcessId(int(tray), _byref(_pid))
            shell_pid = _pid.value
        except Exception:
            shell_pid = 0

        def _in(name):
            rc = rects.get(name)
            return bool(rc) and rc[0] <= x < rc[2] and rc[1] <= y < rc[3]

        if _in("Start"):
            return {"ok": True, "method": "taskbar", "action": "start",
                    "detail": "开始按钮：v1 最佳努力（开始菜单需真实输入通道）"}
        if _in("TrayShowDesktopButtonWClass"):
            apps = _list_app_windows(hdesk, exclude_pid=shell_pid)
            visible = [a for a in apps if not a[3]]
            if visible:
                for a in visible:
                    try:
                        _USER32.ShowWindow(a[0], _SW_MINIMIZE)
                    except Exception:
                        pass
                return {"ok": True, "method": "taskbar",
                        "action": "show_desktop_minimize",
                        "detail": "已最小化 %d 个应用窗" % len(visible)}
            if apps:
                for a in apps:
                    try:
                        _USER32.ShowWindow(a[0], _SW_RESTORE)
                    except Exception:
                        pass
                return {"ok": True, "method": "taskbar",
                        "action": "show_desktop_restore",
                        "detail": "已恢复 %d 个应用窗" % len(apps)}
            return {"ok": True, "method": "taskbar", "action": "show_desktop",
                    "detail": "无应用窗可切换"}
        if _in("MSTaskListWClass") or _in("TrayNotifyWnd"):
            _idx_act = _taskbar_button_index_action(hdesk, x, y, desktop_name)
            if _idx_act is not None:
                return _idx_act
            apps = _list_app_windows(hdesk, exclude_pid=shell_pid)
            if apps:
                if len(apps) == 1:
                    pick = apps[0]
                    note = "已激活 %s" % pick[2]
                else:
                    # 多应用：无法精确知道点了哪个按钮（COM 不可用，按钮自绘无子 HWND）；
                    # M8c: 优先还原 iconic（最小化）窗，否则取 Z 序最前
                    _ic = [a for a in apps if a[3]]
                    pick = _ic[0] if _ic else next((a for a in apps if not a[3]), apps[0])
                    note = "已激活 %s（任务栏共 %d 个应用按钮，取最前）" % (
                        pick[2], len(apps))
                try:
                    if pick[3]:
                        _USER32.ShowWindow(pick[0], _SW_RESTORE)
                    _USER32.SetForegroundWindow(pick[0])
                except Exception as e:
                    return {"ok": True, "method": "taskbar",
                            "action": "activate",
                            "detail": "激活失败: %r" % e}
                return {"ok": True, "method": "taskbar",
                        "action": "activate", "detail": note}
            pinned = _pinned_taskbar_apps()
            if pinned:
                exe = pinned[0]
                if desktop_name:
                    try:
                        win_process.create_process_on_desktop(
                            desktop_name, exe, args="", wait_window_ms=2000)
                        return {"ok": True, "method": "taskbar",
                                "action": "launch_pinned",
                                "detail": "已启动固定项 %s" % exe}
                    except Exception as e:
                        return {"ok": True, "method": "taskbar",
                                "action": "launch_pinned",
                                "detail": "固定项启动失败: %r" % e}
                return {"ok": True, "method": "taskbar", "action": "pinned",
                        "detail": "固定项 %s（v1 未自动启动）" % exe}
            return {"ok": True, "method": "taskbar", "action": "app_button",
                    "detail": "任务栏无运行中/固定应用"}
    except Exception as e:
        return {"ok": True, "method": "taskbar", "action": "error",
                "detail": repr(e)}
    return None


def _maybe_taskbar_action(hdesk, x, y, top, params):
    """M7 #4：点击落在任务栏（Shell_TrayWnd）时做等价动作并短路 click_chain；
    非任务栏（或未识别区域）返回 None → 调用方保持旧行为。"""
    try:
        if not top or _class_name(top) != "Shell_TrayWnd":
            return None
    except Exception:
        return None
    return _taskbar_click_action(hdesk, x, y, top,
                                 (params or {}).get("desktop", ""))


# ---------------- handlers ----------------

def handle_desktop_create(params):
    name = _require(params, "name")
    exe = _require(params, "exe")
    args = params.get("args") or []
    if isinstance(args, list):
        args = " ".join(str(a) for a in args)
    # 大脑 0205ec96b216：探针 62760ae70b9a PASS → withShell 默认 true，
    # vdesk 自带 explorer（壁纸/任务栏/开始菜单；表面可见性受环境限制，可点）
    with_shell = bool(params.get("withShell", True))
    hdesk = win_desktop.create_desktop(name)
    win_desktop.set_thread_desktop(hdesk)
    _SESSIONS[name] = hdesk
    info = win_process.create_process_on_desktop(
        name, exe, args=args, wait_window_ms=5000)
    shell_pid = None
    shell_ready = False
    shell_err = None
    if with_shell:
        # DB-003 修复（大脑 353e419d459d 段7，方案a 复用既有 shell）：
        # 对已存在桌面再 create 时不再起第二个 explorer。
        # T7：本模块无 logger，按既有惯例用 _dbg。
        existing = _shell_pids_on_desktop(hdesk)
        if existing:
            shell_pid = sorted(existing)[0]
            shell_ready = True
        else:
            try:
                sp = win_process.create_process_on_desktop(
                    name, r"C:\Windows\explorer.exe", [], wait_window_ms=3000)
                shell_pid = sp.get("pid")
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if _shell_pids_on_desktop(hdesk):
                        shell_ready = True
                        break
                    time.sleep(0.15)
            except Exception as e:  # noqa: BLE001
                _dbg("explorer on vdesk %s failed: %r" % (name, e))
                shell_err = repr(e)
    # 会话心跳：owner 线程 attach hdesk 后建窗，避免主线程持窗撞 170
    # （大脑 de1bc553f2fc 修复版，替换块 2B）
    try:
        cap.start_session_heartbeat(name, hdesk)
    except Exception:
        pass
    return {"name": name, "hDesktop": int(hdesk),
            "pid": info.get("pid"), "hwnd": info.get("hwnd"),
            "windowReady": info.get("windowReady"),
            "withShell": with_shell, "shellPid": shell_pid,
            "shellReady": shell_ready, "shellErr": shell_err,
            "primaryShellPids": sorted(_PRIMARY_SHELL_PIDS or set())}


def handle_desktop_close(params):
    name = params.get("name") or params.get("desktop")
    if not name:
     raise _BadParams("missing required param: name or desktop")
    # 先停会话心跳，避免向已销毁桌面继续 tick（大脑 0aa2669e6cd7 替换块 2C）
    cap.stop_session_heartbeat(name)
    cap.reset_session_state(name)
    h = _SESSIONS.pop(name, None)
    if not h:
        try:
            h = win_desktop.open_desktop(name, DESKTOP_ALL)
        except Exception:
            h = None
    killed_shell: list = []
    vdesk_shell_list: list = []
    primary_excluded = sorted(_capture_primary_shell_pids())
    switched_back = False
    if h:
        # 修复 A（大脑 bdad6e77a8ce 批准，含三条约束）：
        # create 的 set_thread_desktop(hdesk) 副作用使 RPC 主线程仍 attach 在
        # vdesk，此时 GetShellWindow() 返回 vdesk 自己的 Shell_TrayWnd（如
        # pid 4340），双保险 discard 会把 vdesk explorer 误剔 → 孤儿。
        # 约束 1：Default 句柄模块级缓存，不每次 open（防句柄泄漏）。
        # 约束 2：切换失败不阻断 close，只记 warning。
        # 约束 3：仅切换成功后才执行 GetShellWindow 双剔（姿态确认门）。
        try:
            h_default = _ensure_default_desktop_handle()
            if h_default:
                win_desktop.set_thread_desktop(h_default)
                switched_back = True
        except Exception as e:  # noqa: BLE001
            _dbg("close %s switch-to-Default failed: %r" % (name, e))
        # 跨桌面枚举已证可行，不依赖线程姿态
        vdesk_shell = _shell_pids_on_desktop(h)
        vdesk_shell_list = sorted(vdesk_shell)
        vdesk_shell_only = vdesk_shell - _capture_primary_shell_pids()
        # 约束 3：双保险只在切换回 Default 成功后执行
        if switched_back:
            try:
                sh = _USER32.GetShellWindow()
                if sh:
                    pid = ctypes.wintypes.DWORD(0)
                    _USER32.GetWindowThreadProcessId(sh, _byref(pid))
                    if pid.value:
                        vdesk_shell_only.discard(int(pid.value))
            except Exception:
                pass
        try:
            # 不得改用 desktop_processes，会误杀 explorer
            pids = win_process.killable_pids(h) | vdesk_shell_only
            killed_shell = sorted(vdesk_shell_only)
            _dbg("close %s kill_pids=%s killed_shell=%s" % (
                name, sorted(pids), killed_shell))
            if pids:
                win_process.terminate_pids(pids)
        except Exception as e:  # noqa: BLE001
            _dbg("close %s exc=%r %s" % (
                name, e, traceback.format_exc().splitlines()[-1]))
        try:
            win_desktop.close_desktop(h)
        except Exception:
            pass
    return {"closed": True, "name": name,
            "switchedBackToDefault": switched_back,
            "killedShellPids": killed_shell,
            "vdeskShellPidsEnumerated": vdesk_shell_list,
            "primaryShellPidsExcluded": primary_excluded}


def handle_desktop_list(params):
    names = []
    try:
        for n in win_desktop.enumerate_desktops():
            if isinstance(n, str) and n.startswith("dshv-"):
                names.append(n)
    except Exception as e:
        raise RuntimeError("enumerate_desktops failed: " + str(e))
    return {"desktops": names, "count": len(names)}


def handle_capture_shot(params):
    # 大脑 0aa2669e6cd7 替换块 2E：默认 png、透传 session_name、
    # 返回 timing 与 black 像素真值字段
    name = _require(params, "desktop")
    out = _require(params, "out")
    fmt = params.get("fmt") or "png"
    quality = int(params.get("quality") or 80)
    max_long_edge = int(params.get("maxLongEdge") or 1280)
    # M7（用户裁决）：channel=desktop → 壁纸+任务栏+窗口的完整 Windows 桌面；
    # channel=printwindow → 旧行为（黑底+仅应用窗口）。默认 desktop。
    channel = str(params.get("channel") or "desktop").strip().lower()
    full_desktop = channel != "printwindow"
    hdesk = _open_or_get(name)
    win_desktop.set_thread_desktop(hdesk)
    res = cap.capture_dual(
        out, max_long_edge=max_long_edge, fmt=fmt, quality=quality,
        region=None, wait_after_attach=0.8, session_name=name, hdesk=hdesk,
        full_desktop=full_desktop,
        # 大脑 19eaec24efc2 裁决 A：显式句柄，owner 线程 SetThreadDesktop 用
    )
    black_info = res.get("black_info") or {}
    return {"out": res.get("imagePath"),
            "bytes": res.get("bytes", 0),
            "width": res.get("width", 0),
            "height": res.get("height", 0),
            "sourceWidth": res.get("sourceWidth"),
            "sourceHeight": res.get("sourceHeight"),
            "channel": res.get("channel"),
            "black": bool(res.get("black", True)),
            "blackInfo": black_info,
            # T14：#6 埋点随响应透出（纯增量字段，大脑验收要求回传 perf 全文）
            "perf": res.get("perf"),
            "timing": {"captureMs": res.get("captureMs"),
                       "heartbeatMs": res.get("heartbeatMs"),
                       "encodeMs": res.get("encodeMs"),
                       "totalMs": res.get("totalMs")}}


def _exec_type(hdesk, params):
    """DB-007 段B：原 handle_input_type 函数体（纯执行，无 attach）。
    worker 线程已 set_thread_desktop(hdesk)。"""
    text = params.get("text")
    if not text:
        return {"ok": False, "error": "empty text"}
    # 冻结桌面安全：快路径选窗（GetWindowTextW 会阻塞，由 worker 超时兜底）
    top, target = _wait_for_target_fast(hdesk)
    if target and top and target != top:
        try:
            if _class_name(target) == "Edit":
                r = ic.set_text(target, text)
                if r.get("ok"):
                    return {"ok": True, "readback": r.get("readback"),
                            "method": "setwindowtext"}
        except Exception:
            pass
    r = ic.type_chain(text, hwnd_top=top, hwnd_target=target)
    return {"ok": bool(r.get("landed")), "readback": r.get("readback"),
            "method": r.get("method")}


def handle_input_type(params):
    # DB-007 段C：attach 移到 worker 线程，主线程只 submit + timeout
    desktop_name = _require(params, "desktop")
    hdesk = _open_or_get(desktop_name)
    try:
        return cap._input_via_worker(desktop_name, hdesk, _exec_type, params)
    except TimeoutError:
        return {"ok": False, "error": "input timeout"}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def _exec_click(hdesk, params):
    """DB-007 段B：原 handle_input_click 函数体（纯执行，无 attach）。"""
    try:
        x = int(float(params.get("x", 0)))
        y = int(float(params.get("y", 0)))
    except Exception:
        return {"ok": False, "error": "x/y must be numeric"}
    button = params.get("button") or "left"
    # M5b：坐标命中测试优先（同 _exec_event_click）；未命中回退旧选窗
    hit = _hit_test_window(hdesk, x, y)
    if hit:
        top = hit
        target = _click_target_for(hit, x, y)
    else:
        top, target = _pick_top_and_target_fast(hdesk)
    tb = _maybe_taskbar_action(hdesk, x, y, top, params)
    if tb is not None:
        return tb
    r = ic.click_chain(x, y, hwnd_top=top, hwnd_target=target, button=button)
    return {"ok": bool(r.get("landed")), "method": r.get("method")}


def handle_input_click(params):
    # DB-007 段C：同上，走 per-session 输入 worker
    desktop_name = _require(params, "desktop")
    hdesk = _open_or_get(desktop_name)
    try:
        return cap._input_via_worker(desktop_name, hdesk, _exec_click, params)
    except TimeoutError:
        return {"ok": False, "error": "input timeout"}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def _exec_read_text(hdesk, params):
    """DB-007 段B：原 handle_input_read_text 函数体（纯执行，无 attach）。"""
    top, target = _wait_for_target_fast(hdesk)
    if not top:
        return {"ok": False, "text": "", "source": "no_window"}
    if target:
        return {"ok": True, "text": ic.read_text(target),
                "source": "child_edit", "editHwnd": int(target),
                "hwnd": int(top)}
    return {"ok": True, "text": ic.read_text(top),
            "source": "top_window", "hwnd": int(top)}


def handle_input_read_text(params):
    # DB-007 段C：read_text 主体进 worker 超时兜底（旧"冻结桌面
    # GetWindowTextW 仍会阻塞"的文档化限制由此解除：主线程 5s 超时）
    desktop_name = _require(params, "desktop")
    hdesk = _open_or_get(desktop_name)
    try:
        return cap._input_via_worker(desktop_name, hdesk,
                                     _exec_read_text, params)
    except TimeoutError:
        return {"ok": False, "text": "", "source": "input_timeout"}
    except Exception as e:
        return {"ok": False, "text": "", "source": "error",
                "error": repr(e)}


# ---------------- M10a（大脑 4b4281eb01b8 裁决）：非客户区标题栏按钮拦截 --------
# vdesk 上 SendInput 恒 ACCESS_DENIED、postmessage 只认客户区坐标，
# 标准框（WS_CAPTION）标题栏按钮（×/最小化/最大化）落在非客户区，原链路永远按不到；
# 自绘框（VSCode/WPS/Chrome…）标题栏是普通客户区控件，原链路正常，本补丁不改其行为。
_WM_NCHITTEST = 0x0084
_WM_SYSCOMMAND = 0x0112
_WM_CLOSE = 0x0010
_SC_RESTORE = 0xF120
_SC_MINIMIZE = 0xF020
_SC_MAXIMIZE = 0xF030
_HTCLOSE = 4
_HTMINBUTTON = 8
_HTMAXBUTTON = 9
_HTNCBUTTON = 20  # Win10 DWM 标题栏按钮统一回报该码（幻影按钮区也报 20）


class _POINT_NC(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _try_nonclient_action(top_hwnd, x, y):
    """M10a：判定点击点是否落在标准框标题栏按钮上；是则直接发窗口管理
    消息（复用 close_window 的 WM_CLOSE 路径），否则返回 None 走原链路。

    误伤防护（M10b 实测甄别，probe_m10a_nchit.py）：
    1) 20 码（HTNCBUTTON）按 x 偏移映射 ×/最大化/最小化 三按钮带，带外放行；
    2) 幻影甄别：真实 DWM 标题栏（notepad/资源管理器/Chrome）标题栏中点
       回报 HTCAPTION(2)；Electron 自绘标题栏（VSCode）中点回报 HTCLIENT(1)
       —— 仅中点=2 确认是真标题栏按钮，否则放行（自绘×走客户链路）；
    3) 无 WS_CAPTION 的窗口直接放行。
    """
    if not top_hwnd:
        return None
    try:
        style = _USER32.GetWindowLongW(int(top_hwnd), -16)
        if not (style & 0x00C00000):  # WS_CAPTION
            return None
        lp = ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)
        code = int(_USER32.SendMessageW(int(top_hwnd), _WM_NCHITTEST, 0, lp))
    except Exception:
        return None
    # M10c：Win10 1809 最大化标准窗 DWM 幻影区实测 —— min 位报 2(HTCAPTION)，
    # ×位报 20、max 位报 9；故 code==2 的点击也允许进入按钮带判定（下方映射），
    # 但必须同时满足"右端三按钮带 + 标题栏带内 + 中点=2"三重条件，标题栏中点/
    # 左端普通点击（code==2 且 off>135）仍走原链路（激活/拖动/双击最大化不变）。
    if code not in (_HTCLOSE, _HTMINBUTTON, _HTMAXBUTTON, _HTNCBUTTON, 2):
        return None
    if code == 2:
        # M10c：HTCAPTION 仅当落在右端三按钮带 + 窗顶标题栏带内才按按钮处理
        try:
            r = RECT()
            _USER32.GetWindowRect(int(top_hwnd), _byref(r))
        except Exception:
            return None
        off = r.right - x
        if off > 135 or y < r.top or y >= r.top + 40:
            return None
        if off <= 45:
            code = _HTCLOSE
        elif off <= 90:
            code = _HTMAXBUTTON
        else:
            code = _HTMINBUTTON
        # M10c 幻影甄别（同 20 码）：中点=2 才确认真实 DWM 标题栏
        try:
            mid_lp = ((int(r.top) + 13) & 0xFFFF) << 16 | (int(r.left + (r.right - r.left) / 2) & 0xFFFF)
            mid = int(_USER32.SendMessageW(int(top_hwnd), _WM_NCHITTEST, 0, mid_lp))
            if mid != 2:
                return None
        except Exception:
            return None
    if code == _HTNCBUTTON:
        # M10b：20 码不指明具体按钮 —— 按 x 偏移映射三按钮带
        # （Win10 100% DPI 实测：每钮 45px；right-22→20, right-67→9, right-112→8）
        try:
            r = RECT()
            _USER32.GetWindowRect(int(top_hwnd), _byref(r))
        except Exception:
            return None
        off = r.right - x
        if off > 135:  # 三按钮带之外 → 放行原链路
            return None
        if off <= 45:
            code = _HTCLOSE
        elif off <= 90:
            code = _HTMAXBUTTON
        else:
            code = _HTMINBUTTON
        # M10b 幻影甄别：Electron 自绘标题栏（VSCode）幻影按钮区也报 20，
        # 但其标题栏中点回报 HTCLIENT(1)；真实 DWM 标题栏中点回报 HTCAPTION(2)。
        # 中点≠2 → 自绘×，放行走客户链路（零回归）；=2 → 真按钮，拦截。
        try:
            mid_lp = ((int(r.top) + 13) & 0xFFFF) << 16 | (int(r.left + (r.right - r.left) / 2) & 0xFFFF)
            mid = int(_USER32.SendMessageW(int(top_hwnd), _WM_NCHITTEST, 0, mid_lp))
            if mid != 2:
                return None
        except Exception:
            return None
    try:
        if code == _HTCLOSE:
            ok = bool(_USER32.PostMessageW(int(top_hwnd), _WM_CLOSE, 0, 0))
            action = "close"
        elif code == _HTMINBUTTON:
            ok = bool(_USER32.PostMessageW(int(top_hwnd), _WM_SYSCOMMAND,
                                            _SC_MINIMIZE, 0))
            action = "minimize"
        else:  # HTMAXBUTTON：按当前缩放态发还原/最大化
            if _USER32.IsZoomed(int(top_hwnd)):
                ok = bool(_USER32.PostMessageW(int(top_hwnd), _WM_SYSCOMMAND,
                                               _SC_RESTORE, 0))
                action = "restore"
            else:
                ok = bool(_USER32.PostMessageW(int(top_hwnd), _WM_SYSCOMMAND,
                                               _SC_MAXIMIZE, 0))
                action = "maximize"
    except Exception as e:
        return {"ok": False, "method": "nonclient", "action": "error",
                "error": repr(e)}
    return {
        "ok": bool(ok),
        "method": "nonclient",
        "action": action,
        "hitcode": code,
    }


def _exec_event_click(hdesk, params):
    """DB-007 段B：原 input_event kind=click 函数体（帧坐标映射 +
    窗口矩形 target 选择 + click_chain；纯执行，无 attach）。"""
    try:
        x = float(params.get("x", 0))
        y = float(params.get("y", 0))
    except Exception:
        return {"ok": False, "error": "x/y must be numeric"}
    fw = float(params.get("frameWidth") or 0)
    fh = float(params.get("frameHeight") or 0)
    if fw <= 0 or fh <= 0:
        return {"ok": False, "error": "frameWidth/frameHeight required"}
    sw = float(params.get("srcWidth") or fw)
    sh = float(params.get("srcHeight") or fh)
    button = params.get("button", "left")
    # 帧坐标 → 源桌面坐标（语义 B：服务端唯一一次乘算）
    src_x = int(round(x * sw / fw))
    src_y = int(round(y * sh / fh))
    # M11a：光标跟随 —— 任何点击（任务栏短路/标题栏拦截/客户区链路）都先把
    # vdesk 光标与 sidecar 最后已知光标同步到点击点；此前任务栏按钮/标题栏
    # ×/最小/最大 短路 return，last_cursor 从不更新 → 面板光标停在旧位置。
    try:
        _USER32.SetCursorPos(int(src_x), int(src_y))
        ic._set_last_cursor(src_x, src_y)
    except Exception:
        pass
    # M5b：坐标命中测试优先（点击落在包含该点的窗口上）；未命中回退旧选窗
    hit = _hit_test_window(hdesk, src_x, src_y)
    if hit:
        top_hwnd = hit
        target_hwnd = _click_target_for(hit, src_x, src_y)
    else:
        picked = _pick_top_and_target_fast(hdesk)
        top_hwnd, target_hwnd = picked[0], picked[1]
    if not top_hwnd and not target_hwnd:
        return {"ok": False, "error": "no target window"}
    # 窗口矩形只做"点落在 target 内?"的判断；折客户区由 click_chain
    # 的 ScreenToClient 完成（若在此预折再进 click_chain 会被二次折算）
    used_target = False
    click_top, click_target = top_hwnd, None
    if target_hwnd:
        in_target = False
        try:
            r = RECT()
            _USER32.GetWindowRect(target_hwnd, _byref(r))
            in_target = (r.left <= src_x < r.right) and (r.top <= src_y < r.bottom)
        except Exception:
            in_target = True  # 取不到矩形时按 target 处理
        if in_target:
            used_target = True
            click_target = target_hwnd
    # M7 #4：任务栏区域点击 → 等价窗口管理动作（短路消息点击）
    tb = _maybe_taskbar_action(hdesk, src_x, src_y, top_hwnd, params)
    if tb is not None:
        tb["deskX"], tb["deskY"] = src_x, src_y
        tb["topHwnd"] = int(top_hwnd) if top_hwnd else 0
        tb["targetHwnd"] = 0
        tb["usedTarget"] = False
        return tb
    # M10a（大脑 4b4281eb01b8 裁决）：非客户区拦截 —— 点击落在标准框标题栏
    # 按钮（×/最小化/最大化）时直接发窗口管理消息；HTCLIENT/HTCAPTION/拖边/
    # 任务栏区/自绘框不走此分支，零回归。
    nc = _try_nonclient_action(top_hwnd, src_x, src_y)
    if nc is not None:
        nc["deskX"], nc["deskY"] = src_x, src_y
        nc["topHwnd"] = int(top_hwnd) if top_hwnd else 0
        nc["targetHwnd"] = 0
        nc["usedTarget"] = False
        return nc
    # 不在 target 内（或无 target）：落 top，click_chain 内部 ScreenToClient(top)
    try:
        r = ic.click_chain(src_x, src_y, hwnd_top=click_top or top_hwnd,
                           hwnd_target=click_target, button=button)
    except Exception as e:
        return {"ok": False, "error": "click_chain: %r" % e}
    return {
        "ok": bool(r.get("landed")),
        "method": r.get("method"),
        "deskX": src_x,
        "deskY": src_y,
        "usedTarget": used_target,
        "topHwnd": int(top_hwnd) if top_hwnd else 0,
        "targetHwnd": int(click_target) if click_target else 0,
    }


def handle_input_event(params):
    """VDesktopPanel 帧坐标输入事件（大脑 f63445bf56e9 设计，DB-007 worker 化）。

    参数: {desktop, kind(click|type|read_text), x, y(帧坐标), frameWidth,
    frameHeight, srcWidth, srcHeight, button, text}
    映射: 帧坐标 → 源桌面坐标 → 窗口矩形包含判断选 target/top → click_chain。
    DB-007: 三种 kind 全部在 per-session 输入 worker 线程执行（attach 在
    worker 内）；主线程只 submit + 5s 超时，超时回
    {"ok": False, "error": "input timeout"}，主线程永不挂起。
    """
    desktop = params.get("desktop")
    kind = params.get("kind")
    if not desktop:
        return {"ok": False, "error": "missing desktop"}
    if kind == "click":
        fn = _exec_event_click
    elif kind == "type":
        fn = _exec_type
    elif kind == "read_text":
        # b1 #1（大脑 50bee53c19f6 段 I）：追加模式先读当前内容
        fn = _exec_read_text
    else:
        return {"ok": False, "error": "unsupported kind: %s" % kind}
    # 只做缓存/权限校验；attach 由 worker 线程完成
    try:
        hdesk = _open_or_get(desktop)
    except Exception as e:
        return {"ok": False, "error": "open desktop failed: %r" % (e,)}
    try:
        return cap._input_via_worker(desktop, hdesk, fn, params)
    except TimeoutError:
        return {"ok": False, "error": "input timeout"}
    except Exception as e:
        return {"ok": False, "error": repr(e)}




def _exec_keyboard(hdesk, params):
    """vdesk_keyboard pure executor; worker thread already attached."""
    action = (params.get("action") or "type").lower()
    top, target = _pick_top_and_target_fast(hdesk)
    try:
        if action == "type":
            text = params.get("text")
            if not isinstance(text, str):
                return {"ok": False, "error": "keyboard type: text required"}
            if target and top and target != top:
                try:
                    if _class_name(target) == "Edit":
                        r = ic.set_text(target, text)
                        if r.get("ok"):
                            return {"ok": True, "readback": r.get("readback"), "method": "setwindowtext"}
                except Exception:
                    pass
            r = ic.type_chain(text, hwnd_top=top, hwnd_target=target)
            return {"ok": bool(r.get("landed")), "readback": r.get("readback"), "method": r.get("method")}
        if action == "press":
            key = params.get("key")
            if not key:
                return {"ok": False, "error": "keyboard press: key required"}
            repeat = int(params.get("repeat") or 1)
            r = ic.key_chain(str(key), hwnd_top=top, hwnd_target=target, repeat=repeat)
            return {"ok": bool(r.get("landed")), "method": r.get("method"), "vk": r.get("vk"), "levels": r.get("levels")}
        if action == "hotkey":
            keys = params.get("keys")
            if not keys:
                return {"ok": False, "error": "keyboard hotkey: keys list required"}
            r = ic.hotkey_chain([str(k) for k in keys], hwnd_top=top, hwnd_target=target)
            return {"ok": bool(r.get("landed")), "method": r.get("method"), "vks": r.get("vks"), "levels": r.get("levels")}
        return {"ok": False, "error": "unknown keyboard action: " + action}
    except Exception as e:
        return {"ok": False, "error": "keyboard " + action + " failed: " + repr(e)}


def handle_input_keyboard(params):
    desktop_name = _require(params, "desktop")
    hdesk = _open_or_get(desktop_name)
    try:
        return cap._input_via_worker(desktop_name, hdesk, _exec_keyboard, params)
    except TimeoutError:
        return {"ok": False, "error": "input timeout"}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def _exec_mouse_v2(hdesk, params):
    """vdesk_mouse pure executor; worker thread already attached."""
    action = (params.get("action") or "move").lower()
    x = int(params.get("x") or 0)
    y = int(params.get("y") or 0)
    x2 = int(params.get("x2") or 0)
    y2 = int(params.get("y2") or 0)
    steps = int(params.get("steps") or 12)
    delta = int(params.get("delta") or 0)
    # M5b：坐标命中测试优先（drag 以起点命中）；未命中回退旧选窗
    hit = _hit_test_window(hdesk, x, y)
    if hit:
        top = hit
        target = _click_target_for(hit, x, y)
    else:
        top, target = _pick_top_and_target_fast(hdesk)
    if action in ("click", "double_click", "right_click"):
        # M7 #4：任务栏区域点击 → 等价动作
        tb = _maybe_taskbar_action(hdesk, x, y, top, params)
        if tb is not None:
            return tb
    try:
        r = ic.mouse_chain(action, x, y, hwnd_top=top, hwnd_target=target,
                           x2=x2, y2=y2, steps=steps, delta=delta)
        return {"ok": bool(r.get("landed")), "method": r.get("method"), "levels": r.get("levels")}
    except Exception as e:
        return {"ok": False, "error": "mouse " + action + " failed: " + repr(e)}


def handle_input_mouse(params):
    desktop_name = _require(params, "desktop")
    hdesk = _open_or_get(desktop_name)
    try:
        return cap._input_via_worker(desktop_name, hdesk, _exec_mouse_v2, params)
    except TimeoutError:
        return {"ok": False, "error": "input timeout"}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def handle_capture_zoom(params):
    desktop_name = _require(params, "desktop")
    out = _require(params, "out")
    region = params.get("region")
    if region is None:
        raise _BadParams("capture.zoom: region [x,y,w,h] required")
    if isinstance(region, str):
        region = [int(v) for v in region.split(",")]
    else:
        region = [int(v) for v in region]
    scale = float(params.get("scale") or 2.0)
    fmt = params.get("fmt") or "png"
    quality = int(params.get("quality") or 80)
    from dshv.win32 import capture_gdi as cap_gdi
    hdesk = _open_or_get(desktop_name)
    win_desktop.set_thread_desktop(hdesk)
    res = cap_gdi.capture_zoom_desktop(out, region, scale=scale, fmt=fmt, quality=quality)
    res["channel"] = "gdi_zoom"
    return res


def handle_desktop_sweep(params):
    swept = []
    remaining = []
    kill_errors = []
    try:
        names = list(win_desktop.enumerate_desktops())
    except Exception as e:
        raise RuntimeError("enumerate_desktops failed: " + str(e))
    for n in names:
        if not isinstance(n, str) or not n.startswith("dshv-"):
            continue
        # 会话心跳随 sweep 停止（大脑 0aa2669e6cd7 替换块 2D；
        # 机械适配：保留 OS 级枚举以覆盖孤儿桌面，见验收回传）
        cap.stop_session_heartbeat(n)
        cap.reset_session_state(n)
        try:
            h = win_desktop.open_desktop(n, DESKTOP_ALL)
            try:
                # 不得改用 desktop_processes，会误杀 explorer
                pids = win_process.killable_pids(h)
                if pids:
                    win_process.terminate_pids(pids)
            except Exception as e:
                # M2 小修（大脑裁决）：terminate_pids 失败不再静默
                kill_errors.append(f"{n}: {e!r}")
            win_desktop.close_desktop(h)
            swept.append(n)
        except Exception as e:
            remaining.append(n)
            kill_errors.append(f"{n}: open/close failed: {e!r}")
    # 兜底：清掉所有残留会话心跳（替换块 2D）
    cap.stop_all_session_heartbeats()
    return {"swept": swept, "remainingCount": len(remaining), "killErrors": kill_errors}


def handle_save_file(params):
    desktop_name = _require(params, "desktop")
    path = _require(params, "path")
    hdesk = _open_or_get(desktop_name)
    win_desktop.set_thread_desktop(hdesk)
    top, target = _wait_for_target(hdesk)
    if not target and not top:
        raise RuntimeError("no top window on desktop " + str(desktop_name))
    text = ic.read_text(target or top)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return {"ok": True, "path": path, "chars": len(text),
            "bytes": os.path.getsize(path), "readback": text}




def _probe_save_jpg(rgb: bytes, w: int, h: int, path: str):
    from dshv import encode as _enc
    _enc.save_image(path, rgb, w, h, "jpeg", 80)


def _probe_sha(path: str) -> str:
    import hashlib
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def _probe_capture_via_gdi(src_dc_getter, x: int, y: int, w: int, h: int) -> bytes:
    """用 sidecar M0 已验证的三段管线从任意源 DC 抓 RGB。

    src_dc_getter 是个可调用，返回 (hdc, release_fn)。
    """
    import ctypes as _ct
    from ctypes import byref as _byref, c_void_p as _cvoid
    from dshv.win32 import capture_gdi as _cg
    _gdi32 = _ct.windll.gdi32
    dc, release = src_dc_getter()
    try:
        with _cg._Grab(dc, x, y, w, h) as g:
            buf = g.read()
            return _cg._bgr_to_rgb(buf, w, h)
    finally:
        release()


def _probe_launch_target(hdesk, top_hwnd, edit_hwnd, tag, out_dir):
    """对当前状态跑 6 路抓帧，返回 {path, sha} 字典。"""
    import ctypes as _ct
    from ctypes.wintypes import RECT as _RECT
    _u32 = _ct.windll.user32
    from dshv.win32 import capture_gdi as _cg

    out = {}

    def emit(name, rgb, w, h):
        p = os.path.join(out_dir, f"{tag}{name}.jpg")
        _probe_save_jpg(rgb, w, h, p)
        out[name] = {"path": p, "sha": _probe_sha(p), "w": w, "h": h}

    def _err(name, e):
        out[name] = {"error": str(e)}

    # A：桌面 DC（对照基线）
    try:
        def _a():
            dc = _cg._display_dc()
            return dc, (lambda: _ct.windll.gdi32.DeleteDC(dc))
        vx, vy, vw, vh = dpi.virtual_screen_rect()
        rgb = _probe_capture_via_gdi(_a, vx, vy, vw, vh)
        emit("A_desktopdc", rgb, vw, vh)
    except Exception as e: _err("A_desktopdc", e)

    # B：GetWindowDC(notepad 主窗)
    try:
        r = _RECT(); _u32.GetWindowRect(top_hwnd, _ct.byref(r))
        w = r.right - r.left; h = r.bottom - r.top
        def _b():
            dc = _u32.GetWindowDC(top_hwnd)
            return dc, (lambda: _u32.ReleaseDC(top_hwnd, dc))
        rgb = _probe_capture_via_gdi(_b, 0, 0, w, h)
        emit("B_windc_top", rgb, w, h)
    except Exception as e: _err("B_windc_top", e)

    # C：GetDC(Edit 子窗)
    try:
        r = _RECT(); _u32.GetWindowRect(edit_hwnd, _ct.byref(r))
        w = r.right - r.left; h = r.bottom - r.top
        def _c():
            dc = _u32.GetDC(edit_hwnd)
            return dc, (lambda: _u32.ReleaseDC(edit_hwnd, dc))
        rgb = _probe_capture_via_gdi(_c, 0, 0, w, h)
        emit("C_editdc", rgb, w, h)
    except Exception as e: _err("C_editdc", e)

    # D：PrintWindow 基线
    try:
        r = _RECT(); _u32.GetWindowRect(top_hwnd, _ct.byref(r))
        w = r.right - r.left; h = r.bottom - r.top
        def _d():
            dc = _u32.GetDC(top_hwnd)
            _u32.PrintWindow(top_hwnd, dc, 2)
            return dc, (lambda: _u32.ReleaseDC(top_hwnd, dc))
        rgb = _probe_capture_via_gdi(_d, 0, 0, w, h)
        emit("D_printwindow", rgb, w, h)
    except Exception as e: _err("D_printwindow", e)

    # E：WM_PRINT
    try:
        r = _RECT(); _u32.GetWindowRect(top_hwnd, _ct.byref(r))
        w = r.right - r.left; h = r.bottom - r.top
        def _e():
            dc = _u32.GetDC(top_hwnd)
            WM_PRINT = 0x0317
            flags = 0x02 | 0x04 | 0x10 | 0x08 | 0x20
            _u32.SendMessageW(top_hwnd, WM_PRINT, dc, flags)
            return dc, (lambda: _u32.ReleaseDC(top_hwnd, dc))
        rgb = _probe_capture_via_gdi(_e, 0, 0, w, h)
        emit("E_wmprint", rgb, w, h)
    except Exception as e: _err("E_wmprint", e)

    # F：强制重绘后再 PrintWindow
    try:
        r = _RECT(); _u32.GetWindowRect(top_hwnd, _ct.byref(r))
        w = r.right - r.left; h = r.bottom - r.top
        _u32.RedrawWindow(top_hwnd, None, None, 0x0001 | 0x0100 | 0x0080)
        _u32.UpdateWindow(top_hwnd)
        import time as _t; _t.sleep(0.1)
        def _f():
            dc = _u32.GetDC(top_hwnd)
            _u32.PrintWindow(top_hwnd, dc, 2)
            return dc, (lambda: _u32.ReleaseDC(top_hwnd, dc))
        rgb = _probe_capture_via_gdi(_f, 0, 0, w, h)
        emit("F_redraw_pw", rgb, w, h)
    except Exception as e: _err("F_redraw_pw", e)
    return out


def handle_debug_probe_surface(params):
    """临时调试：对指定桌面跑 6 路抓帧探针，两相（空窗 / 输入后）。"""
    import time as _t
    desktop_name = _require(params, "desktop")
    out_dir = params.get("outDir") or r"D:\surface_probe"
    os.makedirs(out_dir, exist_ok=True)
    hdesk = _open_or_get(desktop_name)
    win_desktop.set_thread_desktop(hdesk)
    top, edit = _wait_for_target(hdesk, 5.0)
    if not top:
        raise RuntimeError("no top window on desktop " + desktop_name)
    if not edit:
        raise RuntimeError("no Edit child (notepad classic required)")
    _t.sleep(0.5)
    empty = _probe_launch_target(hdesk, top, edit, "empty", out_dir)
    ic.set_text(edit, "Hello World")
    _t.sleep(1.0)
    typed = _probe_launch_target(hdesk, top, edit, "typed", out_dir)
    cmp = {}
    for k in empty:
        se = empty[k].get("sha"); st = typed.get(k, {}).get("sha")
        cmp[k] = {"empty": se, "typed": st,
                    "diff": bool(se and st and se != st)}
    return {"empty": empty, "typed": typed, "cmp": cmp, "outDir": out_dir}




_PROBE_SRCCOPY = 0x00CC0020
_PROBE_BI_RGB = 0


class _ProbeBMIH(ctypes.Structure):
    _fields = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class _ProbeBMI(ctypes.Structure):
    _fields = [("bmiHeader", _ProbeBMIH), ("bmiColors", ctypes.c_uint32 * 3)]


def _probe_grab_raw(src_dc, x, y, w, h, tag, out_dir):
    """抓取原语：src_dc(x,y,w,h) → 生产已验证 _Grab 三段管线 → RGB jpg + sha。
    （机械适配：大脑 v2 的裸 windll 32bpp DIBSection 形式在 sidecar 进程内
    两种 lppv 变体均返回 0；改用 capture_gdi._Grab——v1 探针/生产管线同一原语。）"""
    from dshv.win32 import capture_gdi as _cg
    with _cg._Grab(src_dc, x, y, w, h) as g:
        buf = g.read()
        rgb = _cg._bgr_to_rgb(buf, w, h)
    path = os.path.join(out_dir, tag + ".jpg")
    from dshv import encode as _enc
    _enc.save_image(path, rgb, w, h, "jpeg", 80)
    with open(path, "rb") as f:
        import hashlib as _hl
        sha = _hl.sha256(f.read()).hexdigest()[:16]
    return {"path": path, "sha": sha, "w": w, "h": h}


def _probe_shot_B(top, tag, out_dir):
    _u = ctypes.windll.user32
    from ctypes.wintypes import RECT as _R
    r = _R(); _u.GetWindowRect(top, ctypes.byref(r))
    w = r.right - r.left; h = r.bottom - r.top
    dc = _u.GetWindowDC(top)
    try: return _probe_grab_raw(dc, 0, 0, w, h, tag, out_dir)
    finally: _u.ReleaseDC(top, dc)


def _probe_shot_C(edit, tag, out_dir):
    _u = ctypes.windll.user32
    from ctypes.wintypes import RECT as _R
    r = _R(); _u.GetWindowRect(edit, ctypes.byref(r))
    w = r.right - r.left; h = r.bottom - r.top
    dc = _u.GetDC(edit)
    try: return _probe_grab_raw(dc, 0, 0, w, h, tag, out_dir)
    finally: _u.ReleaseDC(edit, dc)


def _probe_shot_D(top, tag, out_dir):
    _u = ctypes.windll.user32
    from ctypes.wintypes import RECT as _R
    r = _R(); _u.GetWindowRect(top, ctypes.byref(r))
    w = r.right - r.left; h = r.bottom - r.top
    dc = _u.GetDC(top)
    try:
        _u.PrintWindow(top, dc, 2)
        return _probe_grab_raw(dc, 0, 0, w, h, tag, out_dir)
    finally: _u.ReleaseDC(top, dc)


def _probe_shot_E(top, tag, out_dir):
    _u = ctypes.windll.user32
    from ctypes.wintypes import RECT as _R
    r = _R(); _u.GetWindowRect(top, ctypes.byref(r))
    w = r.right - r.left; h = r.bottom - r.top
    dc = _u.GetDC(top)
    try:
        _u.SendMessageW(top, 0x0317, dc, 0x02|0x04|0x10|0x08)
        return _probe_grab_raw(dc, 0, 0, w, h, tag, out_dir)
    finally: _u.ReleaseDC(top, dc)


def _probe_matrix(top, edit, phase, out_dir):
    out = {}
    for name, fn, arg in [("B_windc", _probe_shot_B, top),
                          ("C_editdc", _probe_shot_C, edit),
                          ("D_pw", _probe_shot_D, top),
                          ("E_wmprint", _probe_shot_E, top)]:
        try: out[name] = fn(arg, f"{phase}{name}", out_dir)
        except Exception as e: out[name] = {"error": str(e)}
    return out


def handle_debug_probe_v2(params):
    import time as _t
    desktop_name = _require(params, "desktop")
    out_dir = params.get("outDir") or r"D:\surface_probe2"
    os.makedirs(out_dir, exist_ok=True)
    hdesk = _open_or_get(desktop_name)
    win_desktop.set_thread_desktop(hdesk)
    top, edit = _wait_for_target(hdesk, 5.0)
    if not top or not edit:
        return {"error": "no top+edit"}

    # 等首绘完成（probe7 已证 t≈1s，给 3s 保险）
    _t.sleep(3.0)

    # 生产通道抽检，确认已绘制（black 应 False）
    from dshv.win32 import capture_dual as _cd
    prod_path = os.path.join(out_dir, "prod_check.jpg")
    try:
        prod = _cd.capture_dual(prod_path, max_long_edge=1280,
                             fmt="jpeg", quality=80, region=None,
                             wait_after_attach=0.8)
    except Exception as e:
        prod = {"error": str(e)}

    baseline = _probe_matrix(top, edit, "baseline", out_dir)
    ic.set_text(edit, "HELLO-WORLD-V2")
    _t.sleep(1.0)
    after = _probe_matrix(top, edit, "after", out_dir)

    # 唤醒机制矩阵
    _u = ctypes.windll.user32
    mechs = [
        ("redraw_top", lambda: _u.RedrawWindow(top, None, None, 0x0001|0x0100|0x0080|0x0400)),
        ("redraw_edit", lambda: _u.RedrawWindow(edit, None, None, 0x0001|0x0100|0x0080)),
        ("wm_paint_top", lambda: _u.SendMessageW(top, 0x000F, 0, 0)),
        ("wm_paint_edit", lambda: _u.SendMessageW(edit, 0x000F, 0, 0)),
        ("invalidate_edit", lambda: (_u.InvalidateRect(edit, None, True), _u.UpdateWindow(edit))),
        ("invalidate_top", lambda: (_u.InvalidateRect(top, None, True), _u.UpdateWindow(top))),
    ]
    wake = {}
    for name, fn in mechs:
        try:
            fn(); _t.sleep(0.3)
            b = _probe_shot_B(top, f"wake{name}_B", out_dir)
            e = _probe_shot_E(top, f"wake{name}_E", out_dir)
            wake[name] = {"B": b.get("sha"), "E": e.get("sha")}
        except Exception as e:
            wake[name] = {"error": str(e)}

    cmp = {}
    for k in baseline:
        if "sha" in baseline[k] and k in after and "sha" in after[k]:
            cmp[k] = {"baseline": baseline[k]["sha"], "after": after[k]["sha"],
                        "diff": baseline[k]["sha"] != after[k]["sha"]}
    return {"prod_check": prod, "baseline": baseline, "after": after,
            "cmp": cmp, "wake": wake, "outDir": out_dir}




def _exec_cursor_query(hdesk, params):
    """M5a: read last-known cursor pos maintained by input_chain."""
    try:
        lc = ic.last_cursor()
    except Exception:
        lc = {"x": 0, "y": 0, "visible": True}
    return {"ok": True, "x": int(lc.get("x", 0)), "y": int(lc.get("y", 0)),
            "visible": bool(lc.get("visible", True))}


def handle_capture_cursor(params):
    desktop_name = _require(params, "desktop")
    hdesk = _open_or_get(desktop_name)
    try:
        return cap._input_via_worker(desktop_name, hdesk, _exec_cursor_query, params)
    except TimeoutError:
        return {"ok": False, "error": "cursor timeout"}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def _exec_desktop_processes(hdesk, params):
    """M7#6: list vdesk top-level app windows."""
    shell_pid = 0
    try:
        tray = _USER32.FindWindowW("Shell_TrayWnd", None)
        if tray:
            _p = ctypes.wintypes.DWORD(0)
            _USER32.GetWindowThreadProcessId(tray, _byref(_p))
            shell_pid = _p.value
    except Exception:
        pass
    # m9d: 资源管理器文件夹窗仅在可见且非最小化时进列表
    apps = _list_app_windows(hdesk, exclude_pid=shell_pid,
                             allow_classes_on_exclude=("CabinetWClass",))
    _kept = []
    for _a in apps:
        if _a[1] == "CabinetWClass":
            try:
                if _a[3] or not _USER32.IsWindowVisible(_a[0]):
                    continue
            except Exception:
                continue
        _kept.append(_a)
    apps = _kept
    out = []
    for h, cls, title, iconic in apps:
        pid = 0
        try:
            _p = ctypes.wintypes.DWORD(0)
            _USER32.GetWindowThreadProcessId(h, _byref(_p))
            pid = _p.value
        except Exception:
            pass
        out.append({"hwnd": int(h), "class": cls, "title": title, "iconic": bool(iconic), "pid": pid})
    return {"ok": True, "processes": out}


def handle_desktop_processes(params):
    desktop_name = _require(params, "desktop")
    hdesk = _open_or_get(desktop_name)
    try:
        return cap._input_via_worker(desktop_name, hdesk, _exec_desktop_processes, params)
    except TimeoutError:
        return {"ok": False, "error": "processes timeout"}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def _exec_desktop_activate(hdesk, params):
    """M7#6: hardened activate."""
    """M7#6 (L10): verdict relaxed — vdesk 线程 GetForegroundWindow() 恒 0，"""
    """故 ok = SetForegroundWindow 返回 true OR (可见且非 iconic)。"""
    hwnd = int(params.get("hwnd") or 0)
    if not hwnd:
        return {"ok": False, "error": "hwnd required"}
    try:
        if _USER32.IsIconic(hwnd):
            _USER32.ShowWindow(hwnd, _SW_RESTORE)
        cur = ctypes.windll.kernel32.GetCurrentThreadId()
        tgt = _USER32.GetWindowThreadProcessId(hwnd, None)
        attached = False
        if tgt and tgt != cur:
            if _USER32.AttachThreadInput(cur, tgt, True):
                attached = True
        set_fg_ok = False
        try:
            _USER32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0003)
            set_fg_ok = bool(_USER32.SetForegroundWindow(hwnd))
            _USER32.SetFocus(hwnd)
        finally:
            if attached:
                _USER32.AttachThreadInput(cur, tgt, False)
        visible = bool(_USER32.IsWindowVisible(hwnd))
        iconic_now = bool(_USER32.IsIconic(hwnd))
        fg = _USER32.GetForegroundWindow()
        ok = bool(set_fg_ok) or (visible and not iconic_now)
        return {"ok": ok, "method": "activate", "hwnd": hwnd,
            "setFgOk": bool(set_fg_ok), "visible": visible,
            "iconic": iconic_now, "fg": int(fg or 0)}
    except Exception as e:
        return {"ok": False, "error": repr(e)}
def handle_desktop_activate(params):
    desktop_name = _require(params, "desktop")
    hdesk = _open_or_get(desktop_name)
    try:
        return cap._input_via_worker(desktop_name, hdesk, _exec_desktop_activate, params)
    except TimeoutError:
        return {"ok": False, "error": "activate timeout"}
    except Exception as e:
        return {"ok": False, "error": repr(e)}



def _exec_desktop_close_window(hdesk, params):
    """M9-close（A 路线）：软关闭单窗——向 hwnd PostMessage WM_CLOSE。

    worker 线程已 attach vdesk，无需主线程切桌面。"""
    hwnd = int(params.get("hwnd") or 0)
    if not hwnd:
        return {"ok": False, "error": "hwnd required"}
    try:
        ok = bool(_USER32.PostMessageW(int(hwnd), 0x0010, 0, 0))  # WM_CLOSE
        return {"ok": True, "method": "close_window", "hwnd": int(hwnd), "posted": ok}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


def handle_desktop_close_window(params):
    """M9-close：desktop.close_window —— 单窗软关闭（开始菜单 × 按钮）。"""
    desktop_name = _require(params, "desktop")
    hdesk = _open_or_get(desktop_name)
    try:
        return cap._input_via_worker(desktop_name, hdesk,
                                     _exec_desktop_close_window, params, timeout_s=3.0)
    except TimeoutError:
        return {"ok": False, "error": "close_window timeout"}
    except Exception as e:
        return {"ok": False, "error": repr(e)}

def _exec_desktop_launch(hdesk, params):
    """M7#6 L18: launch 在 worker 线程内执行，且不等待窗口（cmd 等控制台程序"""
    """在含 conhost 的 vdesk 上 EnumDesktopWindows 会阻塞 >5s，触发 RPC 全链路超时）。"""
    name = params.get("desktop") or params.get("name")
    exe = params.get("exe")
    args = params.get("args") or ""
    if isinstance(args, list):
        args = " ".join(str(a) for a in args)
    res = win_process.create_process_on_desktop(name, exe, args=args, wait_window_ms=0)
    res["desktop"] = name
    res["windowReady"] = False
    return res


def handle_desktop_launch(params):
    """M7#6 L18: launch 走 per-session worker 线程 + 硬超时（避免阻塞 RPC 主线程）。"""
    name = params.get("desktop") or params.get("name")
    if not name:
        raise _BadParams("missing required param: desktop")
    _require(params, "exe")
    hdesk = _open_or_get(name)
    try:
        return cap._input_via_worker(name, hdesk, _exec_desktop_launch, params, timeout_s=5.0)
    except TimeoutError:
        return {"ok": False, "error": "launch timeout"}
    except Exception as e:
        return {"ok": False, "error": repr(e)}
_HANDLERS = {
    "desktop.create": handle_desktop_create,
    "desktop.close": handle_desktop_close,
    "desktop.list": handle_desktop_list,
    "desktop.sweep": handle_desktop_sweep,
    "capture.shot": handle_capture_shot,
    "input.type": handle_input_type,
    "input.click": handle_input_click,
    "input_event": handle_input_event,
    "input.read_text": handle_input_read_text,
}
_HANDLERS["save.file"] = handle_save_file
_HANDLERS["desktop.launch"] = handle_desktop_launch
_HANDLERS["desktop.processes"] = handle_desktop_processes
_HANDLERS["desktop.activate"] = handle_desktop_activate
_HANDLERS["desktop.close_window"] = handle_desktop_close_window
_HANDLERS["debug.probe_surface"] = handle_debug_probe_surface
_HANDLERS["debug.probe_v2"] = handle_debug_probe_v2
_HANDLERS["capture.cursor"] = handle_capture_cursor
_HANDLERS["input.keyboard"] = handle_input_keyboard
_HANDLERS["input.mouse"] = handle_input_mouse
_HANDLERS["capture.zoom"] = handle_capture_zoom


def dispatch(req):
    id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    if not isinstance(method, str):
        return err(id, E_INVALID_REQUEST, "method must be string")
    fn = _HANDLERS.get(method)
    if fn is None:
        return err(id, E_METHOD_NOT_FOUND, "unknown method: " + method)
    try:
        return ok(id, fn(params))
    except _BadParams as e:
        return err(id, E_INVALID_PARAMS, str(e))
    except Exception as e:
        return err(id, E_INTERNAL, str(e), traceback.format_exc())


def serve() -> int:
    """主循环：stdin 逐行读，stdout 逐行回。EOF 退出。"""
    # 大脑 0205ec96b216：主桌面 shell PID 基线必须在主循环前缓存——
    # 进程启动时主线程仍在主桌面；首次 RPC 后主线程可能 attach 到 vdesk，
    # 届时 EnumWindows 枚举到的将是 vdesk 的窗，基线会污染。
    # 值经 create/close 响应回传（stdout 是 RPC 通道，不另打日志防污染）。
    _capture_primary_shell_pids()
    for line in sys.stdin:
        line = line.strip()
        if line.startswith("\ufeff"):
            line = line[1:]
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            _send({"id": None, "error": {"code": E_PARSE, "message": str(e)}})
            continue
        if not isinstance(req, dict):
            _send({"id": None, "error": {"code": E_INVALID_REQUEST,
                                        "message": "request must be object"}})
            continue
        # M10a（大脑 4b4281eb01b8）：serve 循环耗时埋点（stderr，防 P1 挂起盲区）
        _t0 = time.monotonic()
        try:
            dispatch(req)
        finally:
            _ms = int((time.monotonic() - _t0) * 1000)
            try:
                sys.stderr.write("[dshv] dispatch %s %dms\n" % (req.get("method"), _ms))
                sys.stderr.flush()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(serve())
