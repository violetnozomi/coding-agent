"""只读组合取消来源，供运行和工具共用。"""

import time


class CancellationView:
    """只读组合取消来源；内部取消不会反向设置调用方的 Event。"""

    def __init__(self, *sources) -> None:
        self._sources = tuple(source for source in sources if source is not None)

    def is_set(self) -> bool:
        return any(source.is_set() for source in self._sources)

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            time.sleep(0.02 if remaining is None else min(0.02, remaining))
        return True
