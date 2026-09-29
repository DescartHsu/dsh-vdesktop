"""Per-session temp frame storage: LRU keep-20 under ~/.dsh/tmp/vdesk/<sid>."""

import glob
import os
import threading


def session_tmp_dir(session_id: str) -> str:
    base = os.path.join(os.path.expanduser("~"), ".dsh", "tmp", "vdesk")
    d = os.path.join(base, session_id)
    os.makedirs(d, exist_ok=True)
    return d


class FrameStore:
    """Assigns monotonically increasing frame file names and LRU-prunes old ones."""

    def __init__(self, session_id: str, keep: int = 20):
        self.dir = session_tmp_dir(session_id)
        self.keep = keep
        self._n = 0
        self._lock = threading.Lock()

    def next_path(self, ext: str = "jpg") -> str:
        with self._lock:
            self._n += 1
            name = f"{self._n:05d}.{ext}"
        return os.path.join(self.dir, name)

    def prune(self) -> int:
        """Delete oldest frames beyond `keep`. Returns number deleted."""
        files = sorted(
            glob.glob(os.path.join(self.dir, "*.jpg")) + glob.glob(os.path.join(self.dir, "*.png")),
            key=os.path.getmtime,
        )
        excess = len(files) - self.keep
        if excess <= 0:
            return 0
        for f in files[:excess]:
            try:
                os.remove(f)
            except OSError:
                pass
        return excess

    def clear(self) -> None:
        for f in os.listdir(self.dir):
            p = os.path.join(self.dir, f)
            try:
                os.remove(p)
            except OSError:
                pass