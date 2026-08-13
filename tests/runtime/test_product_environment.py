"""Architecture contracts for the canonical full Product environment."""
from __future__ import annotations


def test_agentloop_is_compatibility_subclass_of_product_environment():
    from nz_coder.runtime.loop import AgentLoop, ProductRunEnvironment

    assert AgentLoop is not ProductRunEnvironment
    assert issubclass(AgentLoop, ProductRunEnvironment)


def test_product_composition_never_constructs_agentloop(monkeypatch):
    from nz_coder.runtime import composition
    from nz_coder.runtime.loop import AgentLoop, ProductRunEnvironment

    built = []
    monkeypatch.setattr(
        AgentLoop,
        "__init__",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("product composition must not instantiate AgentLoop")
        ),
    )
    monkeypatch.setattr(
        ProductRunEnvironment,
        "__init__",
        lambda self, *args, **kwargs: built.append((args, kwargs)),
    )

    environment = composition.build_product_environment("production prompt")

    assert type(environment) is ProductRunEnvironment
    assert built and built[0][0] == ("production prompt",)


def test_legacy_build_coding_agent_is_explicit_compatibility(monkeypatch):
    from nz_coder.runtime import composition
    from nz_coder.runtime.loop import AgentLoop

    monkeypatch.setattr(AgentLoop, "__init__", lambda self, *_args, **_kwargs: None)
    agent = composition.build_coding_agent("compatibility prompt")
    assert type(agent) is AgentLoop
