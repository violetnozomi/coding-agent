"""会话写入租约；外层包装与唯一运行 owner 共享同一个真实资源。"""
from __future__ import annotations

import threading
from typing import Callable

from nz_coder.protocol.public_error import PublicError, PublicRuntimeError


def session_busy() -> PublicRuntimeError:
    return PublicRuntimeError(PublicError("session_busy", "Session execution or settlement is in progress.", True))


class SessionRunLease:
    """单次 claim 防止显式借用同一 token 绕过并发禁入。"""

    def __init__(self, key: object, close: Callable[[], None], *, outer_hold: bool = False):
        self.key = key
        self._close = close
        self._outer_hold = outer_hold
        self._claimed = False
        self._settled = False
        self._active = True
        self._lock = threading.RLock()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    def claim(self, key: object) -> None:
        with self._lock:
            if not self._active or self.key != key or self._claimed:
                raise session_busy()
            self._claimed = True

    def finish(self) -> None:
        with self._lock:
            self._settled = True
            self._release_if_finished()

    def close_hold(self) -> None:
        with self._lock:
            self._outer_hold = False
            self._release_if_finished()

    def _release_if_finished(self) -> None:
        if self._active and not self._outer_hold and (self._settled or not self._claimed):
            self._close()
            self._active = False
