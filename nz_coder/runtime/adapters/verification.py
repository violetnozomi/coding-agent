"""Legacy host adapter for focused completion verification."""
from __future__ import annotations

from nz_coder.runtime.core.verification_context import VerificationExecutionContext


def verification_context_from_legacy_host(host) -> VerificationExecutionContext:
    """Bind reflection hooks at the compatibility boundary only."""
    override = vars(host).get("_check_reflection_gate")
    if not callable(override):
        override = None

    async def review(messages: list, content: str) -> str:
        return await host.hooks.handle_no_tool_response_async(
            host,
            messages,
            message=content,
        )

    return VerificationExecutionContext(override=override, review=review)
