"""Stable provider-neutral result envelope for one model turn."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LLMResult:
    """Normalized buffered or streaming model outcome consumed by AgentRunner."""

    content: str | None = None
    tool_calls: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    diagnostic: str | None = None
    needs_compaction: bool = False
    compaction_error: str = ""
    aborted: bool = False
    duration_ms: float = 0.0
    first_token_ms: float | None = None
    attempts: int = 1
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    provider_reported_cost: float | None = None
    cost: float = 0.0
    cost_known: bool = False
    finish_reason: str = ""
    tools_executed_in_stream: bool = False
    tool_outcome: str = ""
    post_tool_stream_error: str = ""
    assistant_error: dict | None = None
    stream_tool_wait_ms: float = 0.0
