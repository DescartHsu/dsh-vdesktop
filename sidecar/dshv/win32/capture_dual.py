"""D1 双通道捕获：显示 DC + 强化心跳（通道 1）/ PrintWindow 逐顶层窗合成（通道 2）。

调用约定：
- 调用 capture_dual 之前，调用线程必须已 SetThreadDesktop(目标桌面)。
- 环境变量 CAPTURE_CHANNEL=auto|display|printwindow 控制模式；auto 时先 display，
  is_black_frame 判黑则自动降级 PrintWindow，结果里 fallback=True。

环境限制（RDP 空闲会话 + 非前台桌面窗口）：
- 在 RDP 客户端空闲时，会话级渲染暂停（见 F2）。此时对 dshv-* 桌面（非前台）的
  窗口截图通道产出的是 DWM 最后一次合成快照，之后内容变化不再产生新帧。
- 具体表现：首次捕获能得到"窗口已绘制"的完整帧；此后对窗口的任何内容变化
  （SetWindowTextW 输入、标题改动、菜单展开）都不会刷新该快照。
- 用户态 API 无法唤醒：实测 PrintWindow(PW_RENDERFULLCONTENT)、WM_PRINT、
  RedrawWindow(RDW_INVALIDATE|RDW_UPDATENOW)、InvalidateRect + UpdateWindow、
  WM_PAINT 六种机制在空闲会话下对非前台桌面窗口全部无效。
- 原始窗口 DC（GetWindowDC / GetDC(Edit)）在空闲会话下返回"首绘前"黑帧，
  比 PrintWindow 更差，不可作为替代通道。
- 影响：e2e 中模型看到的截图是第一帧快照，不是实时状态。视觉验证不可靠。
- 规避：在 RDP 客户端活跃时（用户在场、会话有渲染输出）重跑视觉验证流程。
- 已知影响范围：GUI 内容视觉验证；不影响 input.read_text、文件落盘、桌面生命周期
  管理、RPC 链路、工具注册——这些路径不依赖窗口像素。
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import c_void_p, c_int, c_uint, byref, WINFUNCTYPE
from ctypes.wintypes import HWND, HDC, HBITMAP, DWORD, BOOL, UINT, LPARAM, RECT, POINT

from dshv.win32 import capture_gdi, dpi
from dshv.win32 import desktop as win_desktop
from dshv import encode as dshv_encode

# #6-scale（大脑 0c11408715ef）：Pillow 可选，缺失则纯 Python 回落
try:
    from PIL import Image as _PILImage
    _HAS_PIL = True
except Exception:
    _PILImage = None
    _HAS_PIL = False

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# --- Win32 常量 ---
WS_POPUP = 0x80000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
RDW_INVALIDATE = 0x0001
RDW_ALLCHILDREN = 0x0080
RDW_UPDATENOW = 0x0100
RDW_FRAME = 0x0400
PW_RENDERFULLCONTENT = 0x00000002
BI_RGB = 0
DIB_RGB_COLORS = 0
SRCCOPY = 0x00CC0020

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", DWORD), ("biWidth", c_int), ("biHeight", c_int),
        ("biPlanes", ctypes.c_ushort), ("biBitCount", ctypes.c_ushort),
        ("biCompression", DWORD), ("biSizeImage", DWORD),
        ("biXPelsPerMeter", c_int), ("biYPelsPerMeter", c_int),
        ("biClrUsed", DWORD), ("biClrImportant", DWORD),
    ]

class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", DWORD * 3)]

def _set_proto():
    user32.CreateWindowExW.restype = HWND
    user32.CreateWindowExW.argtypes = [DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                       DWORD, c_int, c_int, c_int, c_int,
                                       HWND, HWND, c_void_p, c_void_p]
    user32.FindWindowW.restype = HWND
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    # NOTE: no argtypes for GetWindowRect here on purpose — process.py passes
    # byref(defs.RECT) while this module uses wintypes.RECT; a typed argtype
    # with one RECT class breaks the other ("expected LP_RECT instance ...").
    user32.EnumWindows.argtypes = [c_void_p, LPARAM]
    user32.EnumChildWindows.argtypes = [HWND, c_void_p, LPARAM]
    user32.PrintWindow.argtypes = [HWND, HDC, UINT]
    user32.PrintWindow.restype = BOOL
    user32.GetWindowDC.restype = HDC
    user32.GetWindowDC.argtypes = [HWND]
    user32.ReleaseDC.argtypes = [HWND, HDC]
    user32.GetClassNameW.argtypes = [HWND, ctypes.c_wchar_p, c_int]
    user32.GetThreadDesktop.restype = c_void_p
    user32.GetThreadDesktop.argtypes = [c_uint]
    gdi32.CreateCompatibleDC.restype = HDC
    gdi32.CreateCompatibleDC.argtypes = [HDC]
    gdi32.CreateDIBSection.restype = HBITMAP
    # 大脑 job 79a81a408d1c（同 fix #8 RECT 模式）：第二参数用 c_void_p，
    # 不再绑本模块 BITMAPINFO 类，避免与 capture_gdi 的 defs.BITMAPINFO 撞型
    gdi32.CreateDIBSection.argtypes = [HDC, ctypes.c_void_p, UINT,
                                       ctypes.POINTER(c_void_p), c_void_p, DWORD]
    gdi32.SelectObject.restype = c_void_p
    gdi32.SelectObject.argtypes = [HDC, c_void_p]
    gdi32.BitBlt.argtypes = [HDC, c_int, c_int, c_int, c_int, HDC, c_int, c_int, DWORD]
    gdi32.DeleteObject.argtypes = [c_void_p]
    gdi32.DeleteDC.argtypes = [HDC]

_set_proto()

def _create_dib(w: int, h: int):
    """返回 (hdc, hbm, ppv)，画布 32bpp top-down BGRA。"""
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB
    ppv = c_void_p()
    hdc = gdi32.CreateCompatibleDC(None)
    hbm = gdi32.CreateDIBSection(hdc, byref(bmi), DIB_RGB_COLORS, byref(ppv), None, 0)
    if not hbm:
        raise OSError(f"CreateDIBSection failed: {kernel32.GetLastError()}")
    gdi32.SelectObject(hdc, hbm)
    return hdc, hbm, ppv

def _bgra_to_rgb(raw: bytes, w: int, h: int) -> bytes:
    arr = bytearray(raw)
    del arr[3::4]  # 去掉 alpha，剩 BGR BGR ...
    out = bytearray(w * h * 3)
    out[0::3] = arr[2::3]
    out[1::3] = arr[1::3]
    out[2::3] = arr[0::3]
    return bytes(out)

def _class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value

def _scale_rgb(rgb_bytes, src_w, src_h, max_long_edge):
    """缩放 RGB bytes 到 max_long_edge 约束。

    优先 Pillow BILINEAR（本机 12.1.1，预期 1920x1080→1280x720 ~15-30ms）；
    Pillow 缺失或异常时回落纯 Python 最近邻（旧实现，~390-420ms）。
    返回 (scaled_rgb_bytes, sw, sh)。
    （大脑 0c11408715ef 段 B；调用点按 T15 适配新签名/返回值）
    """
    if src_w <= max_long_edge and src_h <= max_long_edge:
        return rgb_bytes, src_w, src_h
    if src_w >= src_h:
        sw = max_long_edge
        sh = int(round(src_h * max_long_edge / float(src_w)))
    else:
        sh = max_long_edge
        sw = int(round(src_w * max_long_edge / float(src_h)))
    if sw < 1: sw = 1
    if sh < 1: sh = 1

    if _HAS_PIL:
        try:
            img = _PILImage.frombuffer(
                "RGB", (src_w, src_h), rgb_bytes, "raw", "RGB", 0, 1)
            img2 = img.resize((sw, sh), _PILImage.BILINEAR)
            return img2.tobytes(), sw, sh
        except Exception:
            pass

    # 纯 Python 最近邻回落（保底）
    out = bytearray(sw * sh * 3)
    x_ratio = src_w / float(sw)
    y_ratio = src_h / float(sh)
    idx = 0
    for y in range(sh):
        sy = int(y * y_ratio)
        row = sy * src_w * 3
        for x in range(sw):
            sx = int(x * x_ratio)
            si = row + sx * 3
            out[idx] = rgb_bytes[si]
            out[idx + 1] = rgb_bytes[si + 1]
            out[idx + 2] = rgb_bytes[si + 2]
            idx += 3
    return bytes(out), sw, sh

# --- DB-005 (大脑 53b66cc4f5b0)：printwindow worker 化 ---
# 主线程卡死 = sidecar RPC 停摆；worker 线程 + 超时让主线程永不卡死。
import queue as _queue
from dshv.win32.desktop import set_thread_desktop  # T10 转录补 import

_PW_WORKERS = {}
_PW_WORKERS_LOCK = threading.Lock()
_PW_TIMEOUT_S = 2.5

class _PwWorker:
    """专用 daemon 线程串行执行 capture_printwindow。

    chrome 等 DWM 应用在节流时 pump 消息停滞，PrintWindow 同步等
    会永久阻塞；worker + timeout 保证主线程不卡死。
    超时后弃用本实例（不 join），下次调用新建。
    """

    def __init__(self, hdesk, name):
        self.name = name
        self._in = _queue.Queue()
        self._out = _queue.Queue()
        self._lock = threading.Lock()
        self._alive = True
        self._seq = 0
        self._attach_err = None
        t = threading.Thread(target=self._run, args=(hdesk,),
                             daemon=True, name="pw-%s" % name)
        self._t = t
        t.start()

    def _run(self, hdesk):
        try:
            if hdesk is not None:
                set_thread_desktop(hdesk)
        except Exception as e:
            self._attach_err = repr(e)
        while True:
            item = self._in.get()
            if item is None:
                return
            tid, args, kwargs = item
            try:
                res = capture_printwindow(*args, **kwargs)
                self._out.put((tid, res, None))
            except Exception as e:
                self._out.put((tid, None, repr(e)))

    def submit(self, args, kwargs, timeout_s):
        with self._lock:
            if not self._alive:
                raise RuntimeError("pw worker dead")
            self._seq += 1
            tid = self._seq
            self._in.put((tid, args, kwargs))
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with self._lock:
                    self._alive = False
                raise TimeoutError("pw worker timeout")
            try:
                got_tid, res, err = self._out.get(timeout=min(remaining, 0.1))
            except _queue.Empty:
                continue
            if got_tid != tid:
                continue
            if err is not None:
                raise RuntimeError(err)
            return res

    def alive(self):
        with self._lock:
            return self._alive and self._t.is_alive()

def _get_pw_worker(name, hdesk):
    key = name if name is not None else "_default"
    with _PW_WORKERS_LOCK:
        w = _PW_WORKERS.get(key)
        if w is None or not w.alive():
            w = _PwWorker(hdesk, key)
            _PW_WORKERS[key] = w
        return w

def _printwindow_via_worker(name, hdesk, out_path, max_long_edge, fmt, quality,
                            full_desktop=False):
    w = _get_pw_worker(name, hdesk)
    return w.submit(
        (out_path, max_long_edge, fmt, quality, full_desktop), {},
        timeout_s=_PW_TIMEOUT_S,
    )


# ---------------- 输入 worker（DB-007，大脑裁决 A；与 _PwWorker 同族，
#                  独立注册表）。通用化：worker 只负责 attach + 执行
#                  callable，不感知 click/type/read_text；callable 签名
#                  fn(hdesk, params) -> dict，由 rpc_stdio 侧提供。
#                  语义约束：输入是操作序列（click/type/read_text），
#                  同一桌面必须串行，故 per-session 单 worker。
#                  超时弃用 worker，下一次调用重建。 ----------------
_INPUT_WORKERS = {}
_INPUT_WORKERS_LOCK = threading.Lock()
_INPUT_TIMEOUT_S = 5.0


class _InputWorker:
    """per-session daemon 线程串行执行 input callable。
    与 _PwWorker 同族：主线程 submit + timeout，超时弃用。
    通用化：worker 不感知 op，接受 fn(hdesk, params) -> dict。
    语义约束：输入是操作序列（click/type/read_text）需同一 worker 串行。"""

    def __init__(self, hdesk, name):
        self.name = name
        self._hdesk = hdesk
        self._in = _queue.Queue()
        self._out = _queue.Queue()
        self._lock = threading.Lock()
        self._alive = True
        self._seq = 0
        self._attach_err = None
        t = threading.Thread(target=self._run, args=(hdesk,),
                             daemon=True, name="input-" + str(name))
        self._t = t
        t.start()

    def _run(self, hdesk):
        try:
            if hdesk is not None:
                win_desktop.set_thread_desktop(hdesk)
        except Exception as e:
            self._attach_err = repr(e)
        while True:
            item = self._in.get()
            if item is None:
                return
            tid, fn, params = item
            try:
                res = fn(hdesk, params)
                self._out.put((tid, res, None))
            except Exception as e:
                self._out.put((tid, None, repr(e)))

    def submit(self, fn, params, timeout_s):
        # T27（DB-008 修复）：等待循环移出 with self._lock，与 _PwWorker.submit
        # 同构；原转录把整个 while 嵌在锁内，超时分支的嵌套 with self._lock
        # 对非可重入 Lock 自死锁 → 主线程永久卡 submit。
        with self._lock:
            if not self._alive:
                raise RuntimeError("input worker dead")
            self._seq += 1
            tid = self._seq
            self._in.put((tid, fn, params))
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with self._lock:
                    self._alive = False
                try:
                    import sys as _sys
                    _sys.stderr.write("[dshv] input worker %s timeout after %dms (seq=%d)\n" % (self.name, int(timeout_s * 1000), tid))
                    _sys.stderr.flush()
                except Exception:
                    pass
                raise TimeoutError("input worker timeout")
            try:
                got_tid, res, err = self._out.get(timeout=min(remaining, 0.1))
            except _queue.Empty:
                continue
            if got_tid != tid:
                continue
            if err is not None:
                raise RuntimeError(err)
            return res

    def alive(self):
        with self._lock:
            return self._alive and self._t.is_alive()


def _get_input_worker(name, hdesk):
    key = name if name is not None else "_default"
    with _INPUT_WORKERS_LOCK:
        w = _INPUT_WORKERS.get(key)
        if w is None or not w.alive():
            try:
                import sys as _sys
                _sys.stderr.write("[dshv] input worker %s respawned\n" % key)
                _sys.stderr.flush()
            except Exception:
                pass
            w = _InputWorker(hdesk, key)
            _INPUT_WORKERS[key] = w
        return w


def _input_via_worker(name, hdesk, fn, params, timeout_s=None):
    w = _get_input_worker(name, hdesk)
    return w.submit(fn, params,
                    timeout_s=timeout_s if timeout_s is not None else _INPUT_TIMEOUT_S)


# ---------------- 会话级心跳单例（大脑 0aa2669e6cd7 替换块 1A，
#                  170 修复版 de1bc553f2fc：owner 线程 attach 后建窗，
#                  主 RPC 线程永不持窗） ----------------
_HEARTBEATS = {}  # name -> {"hb": Heartbeat, "owner": Thread, "stop_evt": Event}
_HEARTBEATS_LOCK = threading.RLock()

def _owner_main(hdesk, hb, stop_evt, ready_evt, ready):
    # 关键：在专用 owner 线程上 attach 目标桌面后再建窗
    try:
        win_desktop.set_thread_desktop(hdesk)
        ready["ok"] = True
    except Exception as e:
        ready["ok"] = False
        ready["err"] = repr(e)
    if ready["ok"]:
        try:
            hb.start()  # CreateWindowExW 落在此 owner 线程
        except Exception as e:
            ready["ok"] = False
            ready["err"] = repr(e)
    ready_evt.set()
    # 自旋等待停止信号
    try:
        stop_evt.wait()
    except Exception:
        pass
    # 关键：在 owner 线程内 stop()，DestroyWindow 落回创建线程
    # （大脑 1527ba854fe1 owner 停止序列）
    destroyed = False
    try:
        destroyed = bool(hb.stop())
    except Exception:
        destroyed = False
    # 仅当窗口已被销毁时，才尝试解绑回 Default（否则会撞 170）；
    # 若窗口残留（没拿到锁），不要切桌面，让线程自然退出
    # （Windows 在线程终止时自动解绑 desktop）
    if destroyed:
        try:
            hdef = win_desktop.open_desktop("Default")
            if hdef:
                win_desktop.set_thread_desktop(hdef)
        except Exception:
            pass

def start_session_heartbeat(name, hdesk):
    with _HEARTBEATS_LOCK:
        ent = _HEARTBEATS.get(name)
        if ent is not None and ent["owner"].is_alive():
            hb0 = ent["hb"]
            if getattr(hb0, "hwnd", None) is not None:
                return hb0
            # owner 活着但窗口没了（异常销毁）：清账重建
            _HEARTBEATS.pop(name, None)
        hb = Heartbeat(interval_s=0.3)
        stop_evt = threading.Event()
        ready_evt = threading.Event()
        ready = {"ok": False, "err": None}
        owner = threading.Thread(
            target=_owner_main, args=(hdesk, hb, stop_evt, ready_evt, ready),
            name="vdesk-hb-owner-%s" % name, daemon=True)
        _HEARTBEATS[name] = {"hb": hb, "owner": owner, "stop_evt": stop_evt}
        owner.start()
        # 等 owner 完成 SetThreadDesktop + hb.start()，
        # 避免主线程 tick 撞空 hwnd
        ready_evt.wait(timeout=2.0)
        if not ready["ok"]:
            # 启动失败：清账，避免半初始化状态被复用
            ent = _HEARTBEATS.get(name)
            if ent is not None and ent["hb"] is hb:
                _HEARTBEATS.pop(name, None)
            raise RuntimeError(
                "start_session_heartbeat failed: %s" % ready["err"])
        return hb

def get_session_heartbeat(name):
    with _HEARTBEATS_LOCK:
        ent = _HEARTBEATS.get(name)
        return ent["hb"] if ent else None

def stop_session_heartbeat(name):
    with _HEARTBEATS_LOCK:
        ent = _HEARTBEATS.pop(name, None)
    if ent is None:
        return
    try:
        ent["stop_evt"].set()  # 唤醒 owner，owner 内 hb.stop() 做 DestroyWindow
    except Exception:
        pass
    try:
        ent["owner"].join(timeout=3.0)
    except Exception:
        pass

def stop_all_session_heartbeats():
    with _HEARTBEATS_LOCK:
        names = list(_HEARTBEATS.keys())
    for n in names:
        stop_session_heartbeat(n)

# ---------------- 冷热状态（替换块 1A 续） ----------------
_COLD_GAP_S = 5.0  # DEPRECATED: 仅保留占位，_is_cold 已改用 owner 存活判据
_BLACK_STREAK_LIMIT = 3
_REVIVE_PROBE_INTERVAL_S = 30.0
_SESSION_STATE = {}  # name -> {"last_ok_ts", "black_streak", "visual_dead"}
_STATE_LOCK = threading.RLock()

def _get_state(name):
    with _STATE_LOCK:
        st = _SESSION_STATE.get(name)
        if st is None:
            st = {"last_ok_ts": 0.0, "black_streak": 0,
                  "visual_dead": False, "last_probe_ts": 0.0}
            _SESSION_STATE[name] = st
        return st

def reset_session_state(name):
    with _STATE_LOCK:
        _SESSION_STATE.pop(name, None)

def _is_cold(name):
    # 判据：心跳 owner 存活 + 心跳窗句柄有效 → 热帧。
    # 不再用"距上次抓帧时长"：owner 0.3s tick，窗口一直活，
    # 时长判据会误判成冷帧，白做一次探针 + 主线程 tick + sleep。
    with _HEARTBEATS_LOCK:
        ent = _HEARTBEATS.get(name)
    if ent is None:
        return True
    if not ent["owner"].is_alive():
        return True
    hb = ent["hb"]
    if getattr(hb, "hwnd", None) is None:
        return True
    return False

def _mark_ok(name):
    st = _get_state(name)
    st["last_ok_ts"] = time.monotonic()
    st["black_streak"] = 0
    st["visual_dead"] = False

def _bump_black(name):
    st = _get_state(name)
    st["black_streak"] += 1
    return st["black_streak"]

def _mark_visual_dead(name):
    st = _get_state(name)
    st["visual_dead"] = True
    st["last_probe_ts"] = time.monotonic()

def _should_revive_probe(name):
    """visual_dead 后每 _REVIVE_PROBE_INTERVAL_S 放行一次真探针。
    返回 True = 本次走一次 printwindow 复活检测；False = 继续短路。"""
    st = _get_state(name)
    now = time.monotonic()
    if (now - st.get("last_probe_ts", 0.0)) >= _REVIVE_PROBE_INTERVAL_S:
        st["last_probe_ts"] = now
        return True
    return False

class Heartbeat:
    """2x2 心跳窗 + 300ms tick（移动 / 闪烁 / RedrawWindow 自身与 Progman/WorkerW）。

    start() 必须在已 SetThreadDesktop(目标桌面) 的线程里调用。
    stop() 幂等，保证单进程 shot 命令可靠结束。
    """

    EXCLUDE_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd"}

    def __init__(self, interval_s: float = 0.3):
        self.interval_s = interval_s
        self.hwnd = None
        self._stop = threading.Event()
        self._tick_lock = threading.RLock()  # 大脑 01d1bb5dc0f5：tick 互斥
        self._thread = None
        self._offset = 0
        self._visible = True
        self._x0 = self._y0 = 0

    def start(self):
        # 建窗 + 首次展示整段持锁，防 tick 线程启动后与 start 尾部交叉
        # （大脑 01d1bb5dc0f5）
        with self._tick_lock:
            if self.hwnd is not None:
                return
            vx, vy, vw, vh = dpi.virtual_screen_rect()
            x = vx + vw - 8 - 2
            y = vy + vh - 8 - 2
            self._x0, self._y0 = x, y
            hwnd = user32.CreateWindowExW(
                WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
                "Static", "", WS_POPUP,
                x, y, 2, 2, None, None, None, None)
            if not hwnd:
                raise OSError(f"CreateWindowExW failed: {kernel32.GetLastError()}")
            self.hwnd = hwnd
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="vdesk-hb-tick")
            self._thread.start()

    def _run(self):
        while not self._stop.wait(self.interval_s):
            try:
                self.tick()
            except Exception:
                pass

    def tick(self):
        # 整段持锁：owner 循环 tick 与主线程显式 tick 互斥
        # （大脑 01d1bb5dc0f5，防同窗 GDI 同步调用锁反转死锁）
        with self._tick_lock:
            if not self.hwnd:
                return
            # 4px 范围内来回移动 1px
            self._offset = (self._offset + 1) % 4
            nx = self._x0 + (1 if self._offset % 2 else -1)
            ny = self._y0
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, nx, ny, 2, 2,
                                SWP_NOACTIVATE | SWP_SHOWWINDOW)
            # 显示/隐藏交替
            self._visible = not self._visible
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE if self._visible else SW_HIDE)
            # 大脑 1527ba854fe1 F2：去掉 RDW_UPDATENOW（同步等 WM_PAINT，
            # 冻结桌面下窗口线程不响应会无限阻塞持锁）；只留异步标脏，
            # 唤醒语义保留
            flags = RDW_INVALIDATE | RDW_FRAME | RDW_ALLCHILDREN
            for h in (self.hwnd,
                      user32.FindWindowW("Progman", None),
                      user32.FindWindowW("WorkerW", None)):
                if h:
                    try:
                        user32.RedrawWindow(h, None, None, flags)
                    except Exception:
                        pass

    def stop(self):
        # 顺序（大脑 01d1bb5dc0f5 + 1527ba854fe1 F3）：
        # stop 事件 → join tick 线程 → 限时拿锁再销毁。
        # 拿不到锁（tick 线程可能还卡在冻结桌面 GDI）就跳过 DestroyWindow，
        # 2x2 顶层窗随 CloseDesktop 一并销毁；返回 destroyed 供诊断。
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        got = False
        try:
            got = self._tick_lock.acquire(timeout=1.5)
        except Exception:
            got = False
        destroyed = False
        if got:
            try:
                if self.hwnd is not None:
                    try:
                        user32.DestroyWindow(self.hwnd)
                        destroyed = True
                    except Exception:
                        pass
                    self.hwnd = None
            finally:
                try:
                    self._tick_lock.release()
                except Exception:
                    pass
        return destroyed

def is_black_frame(rgb: bytes, w: int, h: int) -> dict:
    """64x64 网格最近邻采样，L=0.299R+0.587G+0.114B。

    判黑：mean<5 且 std<2，或 暗像素(L<8)占比>0.99。
    """
    if w <= 0 or h <= 0 or len(rgb) < w * h * 3:
        return {"black": True, "mean": 0.0, "std": 0.0, "dark_ratio": 1.0, "samples": 0}
    sx = max(1, w // 64)
    sy = max(1, h // 64)
    n = 0
    s = 0.0
    s2 = 0.0
    dark = 0
    for y in range(0, h, sy):
        base = y * w
        for x in range(0, w, sx):
            i = (base + x) * 3
            r = rgb[i]; g = rgb[i + 1]; b = rgb[i + 2]
            L = 0.299 * r + 0.587 * g + 0.114 * b
            s += L
            s2 += L * L
            if L < 8:
                dark += 1
            n += 1
    if n == 0:
        return {"black": True, "mean": 0.0, "std": 0.0, "dark_ratio": 1.0, "samples": 0}
    mean = s / n
    var = max(0.0, s2 / n - mean * mean)
    std = var ** 0.5
    dr = dark / n
    black = (mean < 5.0 and std < 2.0) or (dr > 0.99)
    return {"black": black, "mean": mean, "std": std,
            "dark_ratio": dr, "samples": n}

def _probe_rgb_small(max_dim: int = 320):
    """抓一份小尺寸原始像素用于判黑；失败抛异常。"""
    vx, vy, vw, vh = dpi.virtual_screen_rect()
    if vw <= 0 or vh <= 0:
        raise OSError(f"bad virtual screen {vx},{vy},{vw},{vh}")
    scale = min(1.0, max_dim / max(vw, vh))
    sw = max(1, int(vw * scale))
    sh = max(1, int(vh * scale))
    dc = capture_gdi._display_dc()
    try:
        with capture_gdi._Grab(dc, vx, vy, vw, vh) as g:
            small = g.scaled(sw, sh)
            try:
                buf = small.read()
            finally:
                small._cleanup()
            rgb = capture_gdi._bgr_to_rgb(buf, sw, sh)
            return rgb, sw, sh
    finally:
        try:
            gdi32.DeleteDC(dc)
        except Exception:
            pass

def _load_wallpaper_dib(vw: int, vh: int):
    """M7（用户裁决：完整 Windows 桌面）：按会话壁纸设置生成满幅 BGRA 画布 DC。

    - 壁纸路径/缩放风格读 HKCU\\Control Panel\\Desktop（Wallpaper /
      WallpaperStyle）；缺失时回落用户与 systemprofile 的 TransposedWallpaper.jpg。
    - WallpaperStyle=3（Fit/适应）→ 居中黑边；其余（Fill/拉伸/覆盖）→ 满幅裁剪。
    - Pillow 缺失或文件不可读 → 返回 None（画布维持黑底，行为同旧版）。
    返回 (hdc, hbm) 或 None；调用方负责 DeleteObject(hbm)+DeleteDC(hdc)。
    """
    import winreg

    def _read_val(subkey, name):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as k:
                v, _ = winreg.QueryValueEx(k, name)
                return v
        except Exception:
            return None

    candidates = []
    wp = _read_val(r"Control Panel\Desktop", "Wallpaper")
    if wp:
        candidates.append(wp)
    import os as _os
    for base in (_os.environ.get("USERPROFILE"),
                 r"C:\Windows\System32\config\systemprofile"):
        if base:
            candidates.append(_os.path.join(
                base, "AppData", "Roaming", "Microsoft", "Windows",
                "Themes", "TransposedWallpaper.jpg"))
    style = _read_val(r"Control Panel\Desktop", "WallpaperStyle")
    try:
        style = int(style) if style is not None else None
    except Exception:
        style = None

    if not _HAS_PIL:
        return None
    for path in candidates:
        if not path or not _os.path.isfile(path):
            continue
        try:
            img = _PILImage.open(path).convert("RGB")
        except Exception:
            continue
        try:
            if style == 3:  # Fit：等比缩放 + 居中黑边
                img2 = img.copy()
                img2.thumbnail((vw, vh), _PILImage.BILINEAR)
                canvas = _PILImage.new("RGB", (vw, vh), (0, 0, 0))
                canvas.paste(img2, ((vw - img2.width) // 2,
                                    (vh - img2.height) // 2))
            else:  # Fill/拉伸/覆盖：满幅裁剪
                sw, sh = img.size
                s = max(vw / float(sw), vh / float(sh))
                nw, nh = max(1, int(sw * s)), max(1, int(sh * s))
                img2 = img.resize((nw, nh), _PILImage.BILINEAR)
                x0 = (nw - vw) // 2
                y0 = (nh - vh) // 2
                canvas = img2.crop((x0, y0, x0 + vw, y0 + vh))
            raw = canvas.convert("RGBA").tobytes("raw", "BGRA")
        except Exception:
            continue
        try:
            hdc, hbm, _ppv = _create_dib_from_raw(vw, vh, raw)
            return (hdc, hbm)
        except Exception:
            continue
    return None


def _create_dib_from_raw(w: int, h: int, raw_bgra: bytes):
    """把 BGRA raw bytes 装进 32bpp top-down DIB，返回 (hdc, hbm, ppv)。"""
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB
    ppv = c_void_p()
    hdc = gdi32.CreateCompatibleDC(None)
    hbm = gdi32.CreateDIBSection(hdc, byref(bmi), DIB_RGB_COLORS,
                                 byref(ppv), None, 0)
    if not hbm:
        gdi32.DeleteDC(hdc)
        raise OSError(f"CreateDIBSection(wallpaper) failed: {kernel32.GetLastError()}")
    gdi32.SelectObject(hdc, hbm)
    buf = (ctypes.c_char * len(raw_bgra)).from_buffer_copy(raw_bgra)
    ctypes.memmove(ppv, buf, len(raw_bgra))
    return hdc, hbm, ppv


def capture_printwindow(out_path, max_long_edge=1280, fmt="jpeg", quality=80,
                        full_desktop=False):
    """PrintWindow 逐顶层窗合成。调用线程须已 SetThreadDesktop。

    full_desktop=True（M7 desktop 通道）：先 Blt 会话壁纸，再把任务栏
    （Shell_TrayWnd）一并合成 → 完整 Windows 桌面外观；False = 旧行为
    （黑底 + 仅应用窗口）。
    """
    _t0 = time.monotonic()
    _tk = {}
    vx, vy, vw, vh = dpi.virtual_screen_rect()
    if vw <= 0 or vh <= 0:
        raise OSError(f"bad virtual screen {vx},{vy},{vw},{vh}")
    hdc_canvas, hbm_canvas, ppv_canvas = _create_dib(vw, vh)
    wallpaper_dc = None
    wallpaper_bm = None
    if full_desktop:
        try:
            wp = _load_wallpaper_dib(vw, vh)
            if wp is not None:
                wallpaper_dc, wallpaper_bm = wp
                gdi32.BitBlt(hdc_canvas, 0, 0, vw, vh,
                             wallpaper_dc, 0, 0, SRCCOPY)
        except Exception:
            wallpaper_dc = None
            wallpaper_bm = None

    # 过滤规则：可见、尺寸 >0、非 Progman/WorkerW、非 2x2 心跳；
    # M7：full_desktop 时纳入 Shell_TrayWnd（任务栏）
    def _keep(hwnd):
        if not user32.IsWindowVisible(hwnd):
            return None
        r = RECT()
        if not user32.GetWindowRect(hwnd, byref(r)):
            return None
        w = r.right - r.left
        h = r.bottom - r.top
        if w <= 0 or h <= 0:
            return None
        cls = _class_name(hwnd)
        if cls in ("Progman", "WorkerW"):
            return None
        if not full_desktop and cls == "Shell_TrayWnd":
            return None
        if w <= 4 and h <= 4:
            return None
        return (hwnd, r, cls)

    hwnds = []

    @WINFUNCTYPE(BOOL, HWND, LPARAM)
    def _enum_cb(hwnd, lparam):
        k = _keep(hwnd)
        if k:
            hwnds.append(k)
        return True

    user32.EnumWindows(_enum_cb, 0)
    _tk["enum"] = time.monotonic()

    try:
        for hwnd, r, cls in reversed(hwnds):  # EnumWindows 是顶→底，反向即底→顶
            ww = r.right - r.left
            wh = r.bottom - r.top
            hdc_w, hbm_w, ppv_w = _create_dib(ww, wh)
            try:
                ok = user32.PrintWindow(hwnd, hdc_w, PW_RENDERFULLCONTENT)
                if not ok:
                    hdc_src = user32.GetWindowDC(hwnd)
                    if hdc_src:
                        gdi32.BitBlt(hdc_w, 0, 0, ww, wh, hdc_src, 0, 0, SRCCOPY)
                        user32.ReleaseDC(hwnd, hdc_src)
                gdi32.BitBlt(hdc_canvas, r.left - vx, r.top - vy, ww, wh,
                             hdc_w, 0, 0, SRCCOPY)
            finally:
                gdi32.DeleteObject(hbm_w)
                gdi32.DeleteDC(hdc_w)

        _tk["pw"] = time.monotonic()
        raw = ctypes.string_at(ppv_canvas, vw * vh * 4)
        _tk["compose"] = time.monotonic()
        rgb = _bgra_to_rgb(raw, vw, vh)
    finally:
        gdi32.DeleteObject(hbm_canvas)
        gdi32.DeleteDC(hdc_canvas)
        if wallpaper_bm is not None:
            try:
                gdi32.DeleteObject(wallpaper_bm)
            except Exception:
                pass
        if wallpaper_dc is not None:
            try:
                gdi32.DeleteDC(wallpaper_dc)
            except Exception:
                pass

    _tk["bgra"] = time.monotonic()
    # 缩放 + 编码
    long_edge = max(vw, vh)
    scale = 1.0
    ow, oh = vw, vh
    if long_edge > max_long_edge:
        scale = max_long_edge / long_edge
        ow = max(1, int(vw * scale))
        oh = max(1, int(vh * scale))
    if (ow, oh) != (vw, vh):
        rgb, ow, oh = _scale_rgb(rgb, vw, vh, max_long_edge)  # T15
    # Q3（大脑 0aa2669e6cd7 替换块 1B）：black 报缩放后像素真值
    black_info = is_black_frame(rgb, ow, oh)
    _tk["scale"] = time.monotonic()
    dshv_encode.save_image(out_path, rgb, ow, oh, fmt, quality)
    _tk["encode"] = time.monotonic()
    _perf = {
        "enum_ms": int((_tk.get("enum", _t0) - _t0) * 1000),
        "pw_ms": int((_tk.get("pw", _t0) - _t0) * 1000),
        "compose_ms": int((_tk.get("compose", _t0) - _t0) * 1000),
        "bgra_ms": int((_tk.get("bgra", _t0) - _t0) * 1000),
        "scale_ms": int((_tk.get("scale", _t0) - _t0) * 1000),
        "encode_ms": int((_tk.get("encode", _t0) - _t0) * 1000),
        "total_ms": int((time.monotonic() - _t0) * 1000),
    }
    return {
        "imagePath": out_path,
        "bytes": os.path.getsize(out_path) if os.path.exists(out_path) else 0,
        "width": ow, "height": oh,
        "sourceWidth": vw, "sourceHeight": vh,
        "scale": scale,
        "origin": [vx, vy],
        "channel": "desktop" if full_desktop else "printwindow",
        "black": bool(black_info["black"]),
        "black_info": black_info,
        "perf": _perf,
    }

def capture_dual(out_path, max_long_edge=1280, fmt="jpeg", quality=80,
                 region=None, wait_after_attach=0.8, session_name=None,
                 hdesk=None, full_desktop=False):
    """D1 主入口。调用线程须已 SetThreadDesktop(目标桌面)。

    （大脑 0aa2669e6cd7 替换块 1C）热帧（session_name 已知且非冷）直走
    printwindow 快速路径，per-call 心跳 0；冷帧/热帧失败走一次冷补救
    （ensure 会话心跳 + 一次 tick + 320 短探针 + display/printwindow 回落）；
    全黑连续 _BLACK_STREAK_LIMIT 次 → 会话 visual_dead 短路。
    session_name=None 保留旧冷帧行为，仅供未改造调用方。
    """
    t0 = time.monotonic()
    mode = (os.environ.get("CAPTURE_CHANNEL") or "auto").strip().lower()
    if mode not in ("auto", "display", "printwindow"):
        mode = "auto"

    # --- 会话视觉已判死：短路 ---
    if session_name is not None:
        st = _get_state(session_name)
        if st["visual_dead"]:
            if not _should_revive_probe(session_name):
                res = {"imagePath": None, "bytes": 0, "width": 0, "height": 0,
                       "channel": "short_circuit",
                       "black": True,
                       "black_info": {"black": True,
                                      "reason": "session_visual_dead"},
                       }
                return _finalize(res, t0, fmt, quality, session_name,
                                 black_reason="session_visual_dead")
            # 复活探测：只走 printwindow 热路径，不走冷路径
            t_cap = time.monotonic()
            try:
                res = _printwindow_via_worker(session_name, hdesk, out_path,
                                              max_long_edge, fmt, quality,
                                              full_desktop)
            except Exception:
                res = None
            capture_ms = int((time.monotonic() - t_cap) * 1000)
            if res is not None and not res.get("black", False):
                res["captureMs"] = capture_ms
                res["heartbeatMs"] = 0
                _mark_ok(session_name)
                return _finalize(res, t0, fmt, quality, session_name)
            res = {"imagePath": None, "bytes": 0, "width": 0, "height": 0,
                   "channel": "short_circuit",
                   "black": True,
                   "black_info": {"black": True,
                                  "reason": "revive_probe_still_black"},
                   }
            return _finalize(res, t0, fmt, quality, session_name,
                             black_reason="revive_probe_still_black")

    # --- 显式 display：保留旧路径（仍用于冷帧/探针诊断） ---
    if mode == "display":
        res = _cold_capture(out_path, max_long_edge, fmt, quality, region,
                            session_name, wait_after_attach, t0, hdesk)
        if res.get("black"):
            _bump_black(session_name) if session_name else None
        else:
            _mark_ok(session_name) if session_name else None
        return _finalize(res, t0, fmt, quality, session_name)

    # --- auto / printwindow ---
    hot = (session_name is not None) and (not _is_cold(session_name))
    if hot:
        # 热帧：直走 printwindow 快速路径
        t_cap = time.monotonic()
        try:
            res = _printwindow_via_worker(session_name, hdesk, out_path,
                                           max_long_edge, fmt, quality, full_desktop)
        except Exception:
            res = None
        capture_ms = int((time.monotonic() - t_cap) * 1000)
        if res is not None and not res.get("black", False):
            res["captureMs"] = capture_ms
            res["heartbeatMs"] = 0
            _mark_ok(session_name)
            return _finalize(res, t0, fmt, quality, session_name)
        # 黑或失败：计数
        streak = _bump_black(session_name)
        if streak >= _BLACK_STREAK_LIMIT:
            _mark_visual_dead(session_name)
            if res is None:
                res = {"imagePath": None, "bytes": 0, "width": 0, "height": 0,
                       "channel": "printwindow",
                       "black": True,
                       "black_info": {"black": True,
                                      "reason": "printwindow_all_black_streak"},
                       }
            res["captureMs"] = capture_ms
            return _finalize(res, t0, fmt, quality, session_name,
                             black_reason="printwindow_all_black_streak")
        # 未达阈值：走一次冷补救
        cold = _cold_capture(out_path, max_long_edge, fmt, quality, region,
                             session_name, wait_after_attach, t0, hdesk)
        if not cold.get("black", True):
            _mark_ok(session_name)
        return _finalize(cold, t0, fmt, quality, session_name)

    # 冷帧：走冷捕获路径
    cold = _cold_capture(out_path, max_long_edge, fmt, quality, region,
                         session_name, wait_after_attach, t0, hdesk)
    if session_name is not None:
        if cold.get("black", True):
            streak = _bump_black(session_name)
            if streak >= _BLACK_STREAK_LIMIT:
                _mark_visual_dead(session_name)
        else:
            _mark_ok(session_name)
    return _finalize(cold, t0, fmt, quality, session_name)


def _cold_capture(out_path, max_long_edge, fmt, quality, region,
                  session_name, wait_after_attach, t0, hdesk=None):
    """冷帧或热帧失败补救：ensure 会话心跳 + 直走 printwindow worker。
    （T13：主线程不做显式 tick，owner 0.3s tick 即唤醒源）

    #6 修正（大脑 1ef31ab27b0c）：删除 320 短探针层。RDP 下非前台 vdesk
    display 通道结构性无用（DWM 不合成），每次判黑必然回落 printwindow，
    属纯开销。_probe_rgb_small 函数本体保留，仅本函数不再调用。

    大脑 19eaec24efc2 裁决（A 方案）沿用：owner 线程 attach 用显式
    OpenDesktopW 句柄 hdesk，hdesk=None 直接报错。
    """
    heartbeat_ms = 0
    if session_name is not None:
        if hdesk is None:
            raise RuntimeError(
                "_cold_capture: hdesk is required when session_name is set; "
                "pass the OpenDesktopW handle from rpc_stdio._open_or_get")
        hb = start_session_heartbeat(session_name, hdesk)
        # T13（待大脑复核）：显式主线程 tick 造成跨线程同步窗口调用
        # （ShowWindow/SetWindowPos 发往 owner 线程，其正停在
        # stop_evt.wait 不 pump）→ 主线程持锁永久阻塞（faulthandler
        # 实证）。恢复大脑 01d1bb5dc0f5 裁决：owner 0.3s tick 即唤醒源，
        # 主线程不做显式 tick。
        heartbeat_ms = 0
    try:
        time.sleep(min(wait_after_attach, 0.3))
    except Exception:
        pass

    # 直走 printwindow worker（display 探针层已删，见 #6）
    t_cap = time.monotonic()
    try:
        res = _printwindow_via_worker(session_name, hdesk, out_path,
                                      max_long_edge, fmt, quality,
                                      full_desktop)
    except Exception:
        res = None
    capture_ms = int((time.monotonic() - t_cap) * 1000)
    if res is None:
        res = {"imagePath": None, "bytes": 0, "width": 0, "height": 0,
               "channel": "printwindow",
               "black": True,
               "black_info": {"black": True, "reason": "printwindow_failed"},
               }
    res["captureMs"] = capture_ms
    res["heartbeatMs"] = heartbeat_ms
    return res


def _finalize(res, t0, fmt, quality, session_name, black_reason=None):
    """补齐 timing / black_info / session 字段（替换块 1C 后半）。"""
    if res is None:
        res = {"imagePath": None, "bytes": 0, "width": 0, "height": 0,
               "channel": "none", "black": True,
               "black_info": {"black": True,
                              "reason": black_reason or "no_frame"},
               }
    if black_reason is not None:
        bi = dict(res.get("black_info") or {})
        bi["reason"] = black_reason
        res["black_info"] = bi
    res["totalMs"] = int((time.monotonic() - t0) * 1000)
    res.setdefault("captureMs", res.get("captureMs"))
    res.setdefault("heartbeatMs", res.get("heartbeatMs"))
    res.setdefault("encodeMs", res.get("encodeMs"))
    res["session"] = session_name
    return res
