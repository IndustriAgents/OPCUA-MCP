"""Lazy, per-client SDK loop; configuration-only clients own no running thread."""

from __future__ import annotations

import threading

from asyncua.sync import ThreadLoop, ThreadLoopNotRunning


class OwnedLoop(ThreadLoop):
    def __init__(self):
        super().__init__(120)
        self._ownership_lock = threading.Lock()
        self._closed = False

    def post(self, operation):
        with self._ownership_lock:
            if self._closed:
                operation.close()
                raise ThreadLoopNotRunning("the OPC UA client loop is closed")
            if not self.is_alive():
                self.start()
        return super().post(operation)

    def stop(self):
        with self._ownership_lock:
            if self._closed:
                return
            self._closed = True
            super().stop()
