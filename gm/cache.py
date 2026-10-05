"""Disk cache of finished (translated) images, so revisiting a chapter costs nothing."""
from __future__ import annotations

import hashlib
import os
import threading
import time

from .config import local_data_dir


class DiskCache:
    def __init__(self, max_mb: int = 800):
        self.dir = os.path.join(local_data_dir(), "cache")
        os.makedirs(self.dir, exist_ok=True)
        self.max_bytes = max_mb * 1024 * 1024
        self._lock = threading.Lock()
        threading.Thread(target=self.prune, daemon=True).start()

    @staticmethod
    def key(*parts: str) -> str:
        h = hashlib.sha1()
        for p in parts:
            h.update(p.encode("utf-8", "ignore"))
            h.update(b"\x00")
        return h.hexdigest()

    def _path(self, key: str) -> str:
        return os.path.join(self.dir, key[:2], key + ".jpg")

    def get(self, key: str) -> bytes | None:
        p = self._path(key)
        try:
            with open(p, "rb") as f:
                data = f.read()
            os.utime(p, None)  # LRU by mtime
            return data
        except OSError:
            return None

    def path_if_exists(self, key: str) -> str | None:
        p = self._path(key)
        return p if os.path.exists(p) else None

    def put(self, key: str, data: bytes) -> str:
        p = self._path(key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp%d" % threading.get_ident()
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
        return p

    def delete(self, key: str) -> None:
        try:
            os.remove(self._path(key))
        except OSError:
            pass

    def prune(self) -> None:
        with self._lock:
            files = []
            total = 0
            for root, _dirs, names in os.walk(self.dir):
                for n in names:
                    p = os.path.join(root, n)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    if n.endswith(".tmp") or ".tmp" in n and time.time() - st.st_mtime > 3600:
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                        continue
                    files.append((st.st_mtime, st.st_size, p))
                    total += st.st_size
            if total <= self.max_bytes:
                return
            for _mtime, size, p in sorted(files):
                try:
                    os.remove(p)
                    total -= size
                except OSError:
                    pass
                if total <= self.max_bytes * 0.8:
                    break
