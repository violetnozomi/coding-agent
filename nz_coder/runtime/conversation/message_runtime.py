"""Message mutation and Provider projection boundary for Runner turns."""
from __future__ import annotations


class LegacyMessageRuntime:
    """Adapt established coding message behavior behind one cohesive owner."""

    def __init__(self, host) -> None:
        self._host = host

    def persist_compaction_exhaustion(self, *args, **kwargs):
        return self._required("_persist_compaction_exhaustion")(*args, **kwargs)

    def bind_assistant_context(self, *args, **kwargs):
        return self._required("_bind_assistant_context")(*args, **kwargs)

    def bind_user_contexts(self, *args, **kwargs):
        return self._required("_bind_user_contexts")(*args, **kwargs)

    def new_message_part(self, *args, **kwargs):
        return self._required("_new_message_part")(*args, **kwargs)

    def publish_event(self, *args, **kwargs):
        return self._required("_emit_session_event")(*args, **kwargs)

    def materialize_llm_result(self, *args, **kwargs):
        return self._required("_materialize_llm_result")(*args, **kwargs)

    def reconcile_llm_result(self, *args, **kwargs):
        return self._required("_reconcile_materialized_llm_result")(*args, **kwargs)

    def retire_message_part(self, *args, **kwargs):
        return self._required("_retire_message_part")(*args, **kwargs)

    def bind_active_processor(self, processor, messages) -> None:
        self._host._active_session_processor = processor
        self._host._active_processor_messages = messages

    def build_api_messages(self, *args, **kwargs):
        return self._required("_build_api_messages")(*args, **kwargs)

    def apply_usage_cost(self, *args, **kwargs):
        return self._required("_apply_usage_cost")(*args, **kwargs)

    def observe_llm_result(self, *args, **kwargs):
        return self._required("_observe_llm_result")(*args, **kwargs)

    def compact_messages(self, *args, **kwargs):
        return self._required("_compact_messages")(*args, **kwargs)

    def stamp_auto_compaction(self, *args, **kwargs):
        return self._required("_stamp_auto_compaction")(*args, **kwargs)

    def inject_api_diagnostic(self, *args, **kwargs):
        return self._required("_inject_api_diagnostic")(*args, **kwargs)

    def _required(self, name: str):
        value = getattr(self._host, name, None)
        if not callable(value):
            raise RuntimeError(f"MessageRuntime is missing required capability {name}")
        return value
