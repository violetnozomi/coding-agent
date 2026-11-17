"""能力层消费的最小原生命令端口；不拥有运行时资源。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess
from typing import Callable, Literal, Protocol


@dataclass(frozen=True)
class LineCommandResult:
    returncode: int
    stderr: str
    stopped_early: bool


class ProcessInterrupted(Exception):
    def __init__(self, reason: Literal["cancelled", "timeout"]):
        self.reason = reason
        super().__init__(
            "Command cancelled." if reason == "cancelled" else "Command timed out."
        )


class ProcessRunner(Protocol):
    @property
    def search_path(self) -> str: ...

    def capture(
        self, argv: tuple[str, ...], *, cwd: Path, timeout: float
    ) -> CompletedProcess[str]: ...

    def lines(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout: float,
        on_line: Callable[[str], bool],
    ) -> LineCommandResult: ...
