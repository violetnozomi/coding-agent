"""Activation policy for declarative Agent roles inside one shared run."""
from __future__ import annotations

from nz_coder.providers import (
    configured_model_capabilities,
    create_provider,
    prompt_family_guidance,
)
from nz_coder.runtime.model_gateway import ModelSelectionRequest, resolve_model_runtime
from nz_coder.runtime.structured_output import build_structured_output_instruction


class ProductionAgentRoleRuntime:
    """Bind model, reasoning and prompt policy for the active AgentSpec."""

    _EFFORT_ALIASES = {
        "minimal": "low",
        "shallow": "low",
        "balanced": "medium",
        "deep": "high",
    }

    def activate(self, host, agent_name: str) -> None:
        graph = getattr(host, "agent_graph", None)
        if graph is None:
            return
        spec = graph.agent(agent_name)
        provider_id = str(spec.provider or host._default_provider_id).strip().lower()
        model_id = str(spec.model or host._default_model_id).strip()
        requested_effort = spec.effort
        if spec.reasoning is not None and requested_effort is None:
            requested_effort = (
                spec.reasoning.max
                if agent_name in host._agent_reasoning_escalated
                and spec.reasoning.max is not None
                else spec.reasoning.default
            )
        requested_variant = self._EFFORT_ALIASES.get(
            str(requested_effort or "").strip().lower(),
            requested_effort,
        )
        runtime_key = (provider_id, model_id)
        runtime = host._provider_runtimes.get(runtime_key)
        if runtime is None:
            shares_default_client = provider_id == host._default_provider_id
            default_runtime = host._provider_runtimes[
                (host._default_provider_id, host._default_model_id)
            ]
            runtime = resolve_model_runtime(
                ModelSelectionRequest(
                    provider_name=provider_id,
                    model_id=model_id,
                    variant=requested_variant,
                    provider=default_runtime.provider if shares_default_client else None,
                    client=default_runtime.client if shares_default_client else None,
                    owns_client=False if shares_default_client else None,
                ),
                provider_factory=create_provider,
            )
            host._provider_runtimes[runtime_key] = runtime
        if (
            provider_id == host._default_provider_id
            and model_id == host._default_model_id
            and requested_variant is None
        ):
            capabilities = host._default_model_capabilities
            request_model_id = host._default_request_model_id
        else:
            capabilities = self._capabilities(
                spec,
                agent_name,
                provider_id,
                model_id,
                requested_effort,
                requested_variant,
            )
            from nz_coder.providers.registry import registry_runtime_model

            registry_model = registry_runtime_model(provider_id, model_id)
            request_model_id = (
                registry_model.api_model_id if registry_model is not None else model_id
            )
        guidance = prompt_family_guidance(capabilities)
        host.provider_id = provider_id
        host.provider = runtime.provider
        host.client = runtime.client
        host.model_id = model_id
        host.request_model_id = request_model_id
        host.model_capabilities = capabilities
        host.model_variant = capabilities.selected_variant
        host.model_runtime = runtime
        host.model_pricing = runtime.pricing
        host._family_guidance = guidance
        prompt = spec.instructions
        if spec.output_schema is not None:
            prompt = f"{prompt}\n\n{build_structured_output_instruction(spec.output_schema)}"
        if guidance and "## Model-family guidance" not in prompt:
            prompt = f"{prompt}\n\n{guidance}"
        host.system_prompt = prompt

    def escalate(self, host, reason: str) -> bool:
        graph = getattr(host, "agent_graph", None)
        if graph is None or not host.current_agent_name:
            return False
        spec = graph.agent(host.current_agent_name)
        profile = spec.reasoning
        if (
            profile is None
            or not profile.escalate_on_revise
            or profile.max is None
            or host.current_agent_name in host._agent_reasoning_escalated
        ):
            return False
        host._agent_reasoning_escalated.add(host.current_agent_name)
        self.activate(host, host.current_agent_name)
        host.tracer.log(
            "agent_reasoning_escalated",
            agent=host.current_agent_name,
            reason=str(reason),
            effort=str(profile.max),
            model=host.model_id,
        )
        host._emit_session_event("agent.reasoning.escalated", {
            "agent": host.current_agent_name,
            "reason": str(reason),
            "effort": str(profile.max),
            "model": host.model_id,
        })
        return True

    @staticmethod
    def _capabilities(
        spec,
        agent_name: str,
        provider_id: str,
        model_id: str,
        requested_effort,
        requested_variant,
    ):
        base = configured_model_capabilities(provider_id, model_id)
        if requested_variant is None:
            return base
        if requested_variant in base.available_variants:
            return configured_model_capabilities(
                provider_id,
                model_id,
                variant=requested_variant,
            )
        if spec.effort is not None:
            choices = ", ".join(base.available_variants) or "none"
            raise ValueError(
                f"Agent '{agent_name}' requested unsupported effort "
                f"'{requested_effort}' for {provider_id}/{model_id}; available: {choices}"
            )
        return base
