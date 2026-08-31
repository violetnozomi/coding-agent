"""Planning and replan policy boundary for one coding run."""
from __future__ import annotations


class LegacyPlanningRuntime:
    """Preserve mature plan behavior while moving ownership out of Runner."""

    def __init__(self, host) -> None:
        self._host = host

    async def generate(self, messages: list) -> None:
        await self._required("_maybe_generate_plan")(messages)

    async def replan(self) -> None:
        await self._required("_maybe_replan")()

    def _required(self, name: str):
        value = getattr(self._host, name, None)
        if not callable(value):
            raise RuntimeError(f"PlanningRuntime is missing required capability {name}")
        return value
