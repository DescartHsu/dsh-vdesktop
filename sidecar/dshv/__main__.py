"""dshv.__main__ — sidecar entry point (one process per desktop).

  python -m dshv --create-desktop <name>      create desktop, attach, serve JSON-RPC on stdio
  python -m dshv --attach-desktop <name>      attach to existing desktop, serve
  python -m dshv --sweep [--prefix dshv-]     orphan desktop sweep, then exit
  python -m dshv --mcp                        M3: MCP stdio mode (stub)

Protocol: line JSON-RPC 2.0 on stdin/stdout (see docs/gui-agent-plan/02).
After a successful attach the sidecar emits the `ready` event, then serves.
On stdin EOF it kills desktop processes, CloseDesktop, and exits 0 (<1s).
"""

import os as _os
import sys as _sys
_sidecar_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _sidecar_root not in _sys.path:
    _sys.path.insert(0, _sidecar_root)

import argparse
import sys
import threading

from . import methods as _methods
from .rpc import E_UNKNOWN_METHOD, E_INTERNAL, emit, error_msg, result_msg, read_requests
from .win32 import desktop as win_desktop
from .win32 import process as win_process

_DESKTOP_ALL = win_desktop.DESKTOP_ALL


def _handler_factory(ctx, state):
    def handle(req: dict):
        rid = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        fn = _HANDLERS.get(method)
        if fn is None:
            return error_msg(rid, E_UNKNOWN_METHOD, f"unknown method: {method!r}")
        try:
            res = fn(ctx, params)
        except _methods.RpcError as e:
            return error_msg(rid, e.code, str(e), e.data)
        except Exception as e:  # noqa: BLE001
            return error_msg(rid, E_INTERNAL, f"{type(e).__name__}: {e}")
        if method == "close" and not (params or {}).get("keepDesktop"):
            # session is over: exit after this reply (parent reaps the process)
            state["exit_after"] = True
        return result_msg(rid, res)

    return handle


_HANDLERS = {
    "ping": _methods.do_ping,
    "screenshot": _methods.do_screenshot,
    "mouse": _methods.do_mouse,
    "keyboard": _methods.do_keyboard,
    "launch": _methods.do_launch,
    "terminate": _methods.do_terminate,
    "close": _methods.do_close,
    "status": _methods.do_status,
    "session": lambda ctx, p: ctx.describe(),
}


def _serve(ctx) -> int:
    from .rpc import RpcError  # re-export check for _handler_factory

    state = {"exit_after": False}
    handle = _handler_factory(ctx, state)

    h = ctx.h_desktop
    emit({
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "ready",
            "httpPort": 0,  # HTTP side comes in M2 (server_http)
            "desktopName": ctx.desktop_name,
            "hDesktop": hex(h),
            "sessionId": ctx.session_id,
            "virtualScreen": list(ctx.virtual),
            "pid": __import__("os").getpid(),
        },
    })

    stop = threading.Event()
    read_requests(sys.stdin, handle, stop)

    # stdin EOF -> self-clean, exit 0 (design 02 §4)
    if not state["exit_after"]:
        try:
            h2 = h or win_desktop.open_desktop(ctx.desktop_name, _DESKTOP_ALL)
            if h2:
                # 不得改用 desktop_processes，会误杀 explorer
                pids = sorted(win_process.killable_pids(h2))
                if pids:
                    win_process.terminate_pids(pids)
                    win_process.close_pids_gently(pids, timeout_ms=1500)
                win_desktop.close_desktop(h2)
                if h2 != h:
                    pass
        except Exception:  # noqa: BLE001
            pass
    emit({"jsonrpc": "2.0", "method": "event",
          "params": {"type": "bye", "desktopName": ctx.desktop_name}})
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="dshv", description=__doc__)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--create-desktop", metavar="NAME")
    g.add_argument("--attach-desktop", metavar="NAME")
    g.add_argument("--sweep", action="store_true")
    g.add_argument("--mcp", action="store_true", help="M3: MCP stdio mode (stub)")
    p.add_argument("--rpc", choices=["stdio"], default=None,
                   help="Run as JSON-RPC server over stdio (blocks until EOF)")
    p.add_argument("--prefix", default="dshv-", help="sweep prefix (with --sweep)")
    p.add_argument("--session-id", default=None)
    a = p.parse_args(argv)

    if getattr(a, "rpc", None) == "stdio":
        from .rpc_stdio import serve
        return serve()

    if a.sweep:
        from .cli import cmd_orphan_sweep

        ns = argparse.Namespace(prefix=a.prefix)
        return cmd_orphan_sweep(ns)

    if a.mcp:
        print('{"ok": false, "error": "mcp mode not implemented yet (M3)"}', flush=True)
        return 1

    name = a.create_desktop or a.attach_desktop
    if a.create_desktop:
        h = win_desktop.create_desktop(name)
    else:
        h = win_desktop.open_desktop(name, _DESKTOP_ALL)
        if not h:
            print('{"ok": false, "error": "desktop_not_found"}', flush=True)
            return 3
    win_desktop.set_thread_desktop(h)

    session_id = a.session_id or name
    ctx = _methods.SessionContext(session_id, name, h)
    return _serve(ctx)


if __name__ == "__main__":
    sys.exit(main())