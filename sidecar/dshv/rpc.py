"""Line-based JSON-RPC 2.0 over stdio.

Protocol (see docs/gui-agent-plan/02): one JSON object per line on stdout;
requests arrive on stdin as JSON-RPC 2.0. Events (ready, process_exit,
dialog_opened) are pushed as JSON lines WITHOUT an "id".

Error codes (negative, our range):
  -32001 bad_request        -32002 unknown_method   -32003 invalid_params
  -32010 session_not_ready  -32012 session_dead
  -32020 process_not_on_desktop
  -32030 capture_failed     -32040 input_failed     -32041 target_not_foreground
  -32050 too_many_sessions  -32060 desktop_create_failed
  -32070 model_no_image_support
  -32099 internal
"""

import json
import sys

# 本文件 E_ 常量是错误码的单一事实来源（D5 裁决）：
# 新增/修改错误码必须在此定义，并同步 docs/gui-agent-plan/02-传输协议与进程拓扑.md 第 3 节表格；
# 其他模块一律引用这些常量，禁止散落硬编码数值。
E_BAD_REQUEST = -32001
E_UNKNOWN_METHOD = -32002
E_INVALID_PARAMS = -32003
E_SESSION_NOT_READY = -32010
E_SESSION_DEAD = -32012
E_PROCESS_NOT_ON_DESKTOP = -32020
E_CAPTURE_FAILED = -32030
E_INPUT_FAILED = -32040
E_TARGET_NOT_FOREGROUND = -32041
E_TOO_MANY_SESSIONS = -32050
E_DESKTOP_CREATE_FAILED = -32060
E_MODEL_NO_IMAGE = -32070
E_INTERNAL = -32099


class RpcError(Exception):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


def _stdout_lock():
    import threading

    _loc = _stdout_lock
    if not hasattr(_loc, "_lk"):
        _loc._lk = threading.Lock()
    return _loc._lk


def emit(obj: dict, stream=None) -> None:
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    with _stdout_lock():
        (stream or sys.stdout).write(line + "\n")
        (stream or sys.stdout).flush()


def result_msg(req_id, result: dict):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def error_msg(req_id, code: int, message: str, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def event_msg(event: str, **payload):
    m = {"jsonrpc": "2.0", "event": event}
    m.update(payload)
    return m


def read_requests(stream, handler, stop_event):
    """Read JSON-RPC lines until EOF; handler(req_dict) -> response dict (or None
    for notification-style no-response). Returns when stdin closes."""
    while True:
        if stop_event.is_set():
            return
        line = stream.readline()
        if not line:
            return  # EOF
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            emit(error_msg(None, E_BAD_REQUEST, f"json: {e}"))
            continue
        try:
            resp = handler(req)
            if resp is not None:
                emit(resp)
        except RpcError as e:
            rid = req.get("id")
            if rid is not None:
                emit(error_msg(rid, e.code, str(e), e.data))
        except Exception as e:  # noqa: BLE001
            rid = req.get("id")
            if rid is not None:
                emit(error_msg(rid, E_INTERNAL, f"{type(e).__name__}: {e}"))