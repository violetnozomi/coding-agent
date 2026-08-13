# Unified Agent Runtime Phase 0–1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish provider-free main/child characterization coverage and the typed runtime contracts required to extract one shared AgentRunner without changing current production behavior.

**Architecture:** Introduce a dependency-free `runtime.core` contract package containing immutable Agent/profile/request/result types, a mutable per-run state owner, event contracts, and Protocol-typed services. Existing `AgentLoop`, `run_subagent`, CLI, HTTP, and SWE-bench entry points remain unchanged in this phase.

**Tech Stack:** Python 3.9+, standard-library dataclasses, enum, pathlib, typing Protocol, pytest; no Agent framework and no new runtime dependency.

## Global Constraints

- Preserve `AgentLoop(...)`, `agent.run(...)`, and `run_subagent(...)` signatures.
- Preserve existing tool names, schemas, result strings, session formats, and trace formats.
- All production modules remain Python 3.9 compatible and use `from __future__ import annotations`.
- Do not introduce LangChain, LlamaIndex, CrewAI, or another Agent framework.
- Do not add external dependencies.
- Use provider fakes only; do not issue paid API requests or run SWE-bench.
- Do not create commits or require a Git workflow.

---

### Task 1: Immutable runtime profiles

**Files:**
- Create: `nz_coder/runtime/core/__init__.py`
- Create: `nz_coder/runtime/core/profiles.py`
- Test: `tests/runtime/core/test_profiles.py`

**Interfaces:**
- Produces: `RunMode`, `RunProfile`, `MAIN_PROFILE`, `READ_CHILD_PROFILE`, `WRITE_CHILD_PROFILE`, `BACKGROUND_PROFILE`, `WORKFLOW_PROFILE`, and `profile_for_mode(mode)`.
- Consumes: no production runtime module.

- [x] **Step 1: Write failing profile validation tests**

```python
def test_read_child_profile_cannot_mutate_or_spawn_children():
    assert READ_CHILD_PROFILE.allow_mutation is False
    assert READ_CHILD_PROFILE.allow_child_agents is False

def test_profile_rejects_empty_name():
    with pytest.raises(ValueError, match="name"):
        RunProfile(name="", mode=RunMode.MAIN)
```

- [x] **Step 2: Run the focused test and verify import failure**

Run: `pytest -q tests/runtime/core/test_profiles.py`

Expected: FAIL because `nz_coder.runtime.core.profiles` does not exist.

- [x] **Step 3: Implement the immutable profiles**

```python
class RunMode(str, Enum):
    MAIN = "main"
    READ_CHILD = "read_child"
    WRITE_CHILD = "write_child"
    BACKGROUND = "background"
    WORKFLOW = "workflow"

@dataclass(frozen=True)
class RunProfile:
    name: str
    mode: RunMode
    allow_mutation: bool = True
    allow_child_agents: bool = False
    interactive_questions: bool = False
    durable_session: bool = True
```

Validate non-empty names and expose canonical constants plus an exhaustive
mode lookup.

- [x] **Step 4: Run profile tests**

Run: `pytest -q tests/runtime/core/test_profiles.py`

Expected: PASS.

### Task 2: Agent definition, request, result, and state ownership

**Files:**
- Create: `nz_coder/runtime/core/request.py`
- Create: `nz_coder/runtime/core/result.py`
- Create: `nz_coder/runtime/core/state.py`
- Modify: `nz_coder/runtime/core/__init__.py`
- Test: `tests/runtime/core/test_models.py`

**Interfaces:**
- Consumes: `RunProfile` from Task 1.
- Produces: `AgentDefinition`, `RunRequest`, `RunStatus`, `TokenUsage`, `RunResult`, and `RunState`.

- [x] **Step 1: Write failing model ownership tests**

```python
def test_request_snapshots_messages_and_tool_names(tmp_path):
    messages = [{"role": "user", "content": "inspect"}]
    request = RunRequest(
        agent=AgentDefinition("worker", "Inspect the repository"),
        profile=MAIN_PROFILE,
        messages=messages,
        workspace=tmp_path,
        session_id="session-1",
        tool_names=["read_file"],
    )
    messages[0]["content"] = "changed"
    assert request.messages[0]["content"] == "inspect"
    assert request.tool_names == ("read_file",)

def test_run_state_is_the_mutable_transcript_owner(request):
    state = RunState.from_request(request)
    state.append_message({"role": "assistant", "content": "done"})
    assert state.turn_count == 0
    assert state.transcript[-1]["content"] == "done"
```

- [x] **Step 2: Run the focused test and verify import failure**

Run: `pytest -q tests/runtime/core/test_models.py`

Expected: FAIL because the model modules do not exist.

- [x] **Step 3: Implement validated contract models**

Use frozen dataclasses for `AgentDefinition`, `RunRequest`, `TokenUsage`, and
`RunResult`. Normalize mutable inputs to immutable snapshots in `__post_init__`.
Use a regular dataclass for `RunState`; it owns a deep-copied transcript,
turn/iteration counters, active agent, terminal status, usage, and correlation
metadata. `RunState.from_request(request)` is the only constructor needed by
future Runner code.

- [x] **Step 4: Run model tests**

Run: `pytest -q tests/runtime/core/test_models.py`

Expected: PASS.

### Task 3: Service and event ports

**Files:**
- Create: `nz_coder/runtime/core/events.py`
- Create: `nz_coder/runtime/core/contracts.py`
- Modify: `nz_coder/runtime/core/__init__.py`
- Test: `tests/runtime/core/test_contracts.py`

**Interfaces:**
- Consumes: `RunRequest`, `RunResult`, and `RunState` from Task 2.
- Produces: `RuntimeEvent`, `RuntimeEventSink`, `ModelGateway`, `ToolRuntime`, `ContextManager`, `SessionRepository`, `MemoryService`, `CompletionVerifier`, and `RuntimeServices`.

- [x] **Step 1: Write failing structural-contract tests**

```python
def test_runtime_services_accept_structural_implementations():
    services = RuntimeServices(
        model=FakeModelGateway(),
        tools=FakeToolRuntime(),
        context=FakeContextManager(),
        sessions=FakeSessionRepository(),
        events=RecordingEventSink(),
    )
    assert isinstance(services.model, ModelGateway)
    assert isinstance(services.tools, ToolRuntime)
```

Each fake implements only the documented Protocol methods. Include a negative
test for a missing required service and an event-payload snapshot test.

- [x] **Step 2: Run the focused test and verify import failure**

Run: `pytest -q tests/runtime/core/test_contracts.py`

Expected: FAIL because service contracts do not exist.

- [x] **Step 3: Implement runtime-checkable Protocol ports**

Define narrow async Protocols:

```python
class ModelGateway(Protocol):
    async def complete(self, request: RunRequest, state: RunState) -> object: ...

class ToolRuntime(Protocol):
    async def execute_batch(self, calls: tuple[dict, ...], request: RunRequest, state: RunState) -> tuple[object, ...]: ...

class ContextManager(Protocol):
    async def prepare(self, request: RunRequest, state: RunState) -> None: ...

class SessionRepository(Protocol):
    async def load(self, request: RunRequest, state: RunState) -> None: ...
    async def save(self, request: RunRequest, state: RunState) -> None: ...
```

`RuntimeServices` validates the five required services and permits optional
memory and verifier ports. It holds no concrete implementation imports.

- [x] **Step 4: Run contract tests**

Run: `pytest -q tests/runtime/core/test_contracts.py`

Expected: PASS.

### Task 4: Production facade characterization

**Files:**
- Test: `tests/runtime/core/test_legacy_facades.py`
- Modify: `docs/infcode-alignment-learning-log.md`

**Interfaces:**
- Consumes: existing `build_coding_agent`, `AgentLoop.run`, `run_subagent_async`, and core profile constants.
- Produces: a locked compatibility baseline; no production implementation change.

- [x] **Step 1: Add a provider-free main facade test**

Construct `build_coding_agent()` with the existing fake client, execute one
non-streaming text-only turn, and assert the result remains a dictionary with
the current `status` and `runtime.profile == "coding"` shape. Assert that the
caller-owned message list receives the assistant message in place; the current
result dictionary intentionally has no `messages` key.

- [x] **Step 2: Add a child facade signature contract**

Use `inspect.signature(run_subagent)` and assert the current public parameters
remain present: `prompt`, `agent_type`, `session_id`, `allowed_tools`,
`target_paths`, `cancel_event`, `output_schema`, `model_hint`, `evidence_refs`,
and `verification`. This phase intentionally does not invoke a child Provider
loop.

- [x] **Step 3: Assert canonical profile policy matches existing surfaces**

Assert main is interactive and mutation-capable, read-child is non-interactive
and read-only, write-child permits scoped mutation, and background is
non-interactive. These tests become the profile matrix used by later shared
Runner contract tests.

- [x] **Step 4: Run facade and existing composition/child tests**

Run: `pytest -q tests/runtime/core/test_legacy_facades.py tests/test_runtime_composition.py tests/test_child_contracts.py tests/test_subagent.py`

Expected: PASS.

- [x] **Step 5: Record the alignment state truthfully**

Append an entry to `docs/infcode-alignment-learning-log.md` stating that the
architecture is approved and Phase 0–1 is contract-only. Mark unified Runner,
Provider extraction, tool pipeline extraction, context/session migration, and
child-loop removal as not yet wired.

### Task 5: Verification and self-review

**Files:**
- Verify all files created or modified by Tasks 1–4.

**Interfaces:**
- Consumes: all Phase 0–1 deliverables.
- Produces: a tested contract foundation and an explicit boundary for Phase 2.

- [x] **Step 1: Run focused tests**

Run: `pytest -q tests/runtime/core tests/test_runtime_composition.py tests/test_child_contracts.py tests/test_subagent.py`

Expected: PASS.

- [x] **Step 2: Run static syntax and lint checks**

Run: `python -m compileall -q nz_coder/runtime/core tests/runtime/core`

Run: `ruff check nz_coder/runtime/core tests/runtime/core`

Expected: both commands exit 0.

- [x] **Step 3: Run the complete test suite**

Run: `pytest -q`

Expected: PASS with no newly introduced failure.

- [x] **Step 4: Check dependency isolation**

Run: `python -c "import nz_coder.runtime.core; print('runtime-core-import-ok')"`

Expected: `runtime-core-import-ok`. Inspect `runtime/core` imports and confirm it
does not import `interface`, concrete providers, tools, sessions, AgentLoop, or
subagent modules.

- [x] **Step 5: Review the Phase 2 boundary**

Confirm no production facade delegates to the new contracts yet. Phase 2 may
begin only by adapting the existing Provider call path to `ModelGateway`; it
must not introduce a second new loop.
