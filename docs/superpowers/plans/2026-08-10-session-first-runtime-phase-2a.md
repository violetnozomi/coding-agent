# Session-First Runtime Phase 2A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put a durable Session and one RunContext into NZ-Coder's real production Runner path while preserving current CLI, HTTP, SDK, evaluation, and child behavior.

**Architecture:** Introduce a small `runtime/session` domain whose Session owns the complete mutable transcript and whose store adapter preserves the current JSON format. `AgentRunner.run()` will open a `RunContext` through a SessionRuntime, run the existing turn state machine against the Session-owned transcript, and checkpoint/finalize through the same object; `AgentLoop` remains a temporary LegacyHostAdapter during this phase.

**Tech Stack:** Python 3.9+, asyncio, standard-library dataclasses/Protocol/pathlib/copy, pytest, Ruff; no new runtime dependency and no Agent framework.

**Execution status (2026-08-10):** Complete. All seven tasks below were
implemented and verified. Two design corrections discovered by the red-green
cycle supersede the illustrative snippets: a terminal *run* does not permanently
close its Session, and `SessionProcessor` reports stable mutations by marking the
Session dirty rather than forcing a disk write for every update. The durable
checkpoint boundary remains `SessionRuntime`.

## Global Constraints

- Preserve the current public `AgentLoop.run(messages, ...)`, `AgentClient.run(request)`, and `run_subagent(...)` signatures.
- Preserve the current on-disk session JSON shape and current message dictionaries.
- All production files use `from __future__ import annotations` and module docstrings.
- Do not issue paid Provider requests or run SWE-bench.
- Do not hide failures with broad exception suppression.
- Do not delete compatibility code until its consumer/deletion gate passes.
- Do not commit or create branches: the worktree contains mixed user changes and the user has not requested Git mutations.

## File structure

- Create `nz_coder/runtime/session/model.py`: Session identity, parent relation, metadata, status, and transcript ownership.
- Create `nz_coder/runtime/session/store.py`: storage-neutral `SessionStore` Protocol and legacy JSON adapter.
- Create `nz_coder/runtime/session/runtime.py`: open/checkpoint/finalize orchestration and legacy-host request projection.
- Create `nz_coder/runtime/session/__init__.py`: intentional public runtime-session exports.
- Create `nz_coder/runtime/core/run_context.py`: single-run mutable owner composed from Session and immutable request/config facts.
- Modify `nz_coder/runtime/runner.py`: open and close RunContext in the production path; keep the current host adapter temporarily.
- Modify `nz_coder/runtime/core/contracts.py`: replace checkpoint-only SessionRepository semantics with the SessionRuntime port while retaining a compatibility alias during migration.
- Modify `nz_coder/runtime/services.py`: compose the production SessionRuntime.
- Modify `nz_coder/runtime/session_repository.py`: become a compatibility import/adapter, not a second storage implementation.
- Modify `nz_coder/runtime/session_processor.py`: publish mutations through an injected message sink without changing existing dictionary behavior.
- Add focused tests under `tests/runtime/session/` and extend `tests/runtime/test_runner.py` and `tests/runtime/test_sdk.py`.

---

### Task 1: Freeze current production Session behavior

**Files:**
- Create: `tests/runtime/session/test_characterization.py`
- Modify: `tests/runtime/test_runner.py`

**Interfaces:**
- Consumes: existing `AgentRunner.run(host, messages, ...)`, `save_session`, `load_session`, and message identity helpers.
- Produces: characterization gates for caller-list identity, checkpoint order, terminal status, resume, and durable message parts. Parent metadata is intentionally absent in the legacy baseline and is added by Task 3.

- [x] **Step 1: Add a caller-list and checkpoint characterization test**

```python
def test_runner_preserves_caller_list_and_checkpoints_running_then_terminal():
    messages = [{"role": "user", "content": "work"}]
    sessions = RecordingSessions()
    host = SettledHost()
    result = asyncio.run(AgentRunner(_services(host, sessions=sessions)).run(
        host, messages, stream=False,
    ))
    assert result == {"status": "max_turns"}
    assert messages == [{"role": "user", "content": "work"}]
    assert sessions.statuses == ["running"]
```

- [x] **Step 2: Add legacy JSON round-trip characterization**

```python
def test_legacy_session_round_trip_preserves_status_and_message_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path / ".nz-coder" / "sessions")
    messages = [{
        "role": "assistant",
        "content": "done",
        "_message_id": "msg-1",
        "_parts": [{"id": "part-1", "type": "text", "text": "done"}],
    }]
    save_session(messages, session_id="child-1", run_status="completed")
    payload = load_session("child-1")
    assert payload["messages"] == messages
    assert payload["run_status"] == "completed"
```

- [x] **Step 3: Run the characterization tests**

Run: `pytest -q tests/runtime/session/test_characterization.py tests/runtime/test_runner.py`

Expected: PASS against the current implementation. A failure is a baseline discrepancy to understand before production edits.

- [x] **Step 4: Record a no-commit checkpoint**

Run: `git diff --check -- tests/runtime/session/test_characterization.py tests/runtime/test_runner.py`

Expected: no output.

### Task 2: Add the Session domain model

**Files:**
- Create: `nz_coder/runtime/session/__init__.py`
- Create: `nz_coder/runtime/session/model.py`
- Create: `tests/runtime/session/test_model.py`

**Interfaces:**
- Consumes: `RunStatus` and existing message dictionaries.
- Produces: `SessionIdentity`, `Session`, `SessionStatus`, `Session.append()`, `Session.replace_transcript()`, `Session.checkpoint()`, and `Session.snapshot()`.

- [x] **Step 1: Write failing ownership tests**

```python
def test_session_copies_initial_messages_but_owns_live_transcript():
    source = [{"role": "user", "content": "inspect"}]
    session = Session.create("session-1", source, workspace=Path("."))
    source[0]["content"] = "mutated"
    session.append({"role": "assistant", "content": "done"})
    assert session.transcript[0]["content"] == "inspect"
    assert session.transcript[-1]["content"] == "done"

def test_session_rejects_append_after_terminal():
    session = Session.create("session-1", [], workspace=Path("."))
    session.finish(SessionStatus.COMPLETED)
    with pytest.raises(RuntimeError, match="terminal"):
        session.append({"role": "user", "content": "late"})
```

- [x] **Step 2: Run tests and verify the new module is absent**

Run: `pytest -q tests/runtime/session/test_model.py`

Expected: FAIL with `ModuleNotFoundError` for `nz_coder.runtime.session.model`.

- [x] **Step 3: Implement the minimal Session model**

```python
@dataclass(frozen=True)
class SessionIdentity:
    session_id: str
    parent_session_id: str | None = None

@dataclass
class Session:
    identity: SessionIdentity
    workspace: Path
    transcript: list[dict]
    status: SessionStatus = SessionStatus.IDLE
    metadata: dict = field(default_factory=dict)
    usage: TokenUsage = field(default_factory=TokenUsage)

    @classmethod
    def create(cls, session_id, messages, *, workspace, parent_session_id=None, metadata=None):
        identity = SessionIdentity(session_id, parent_session_id)
        return cls(
            identity=identity,
            workspace=Path(workspace).resolve(),
            transcript=copy.deepcopy(list(messages)),
            metadata=copy.deepcopy(metadata or {}),
        )

    def append(self, message: dict) -> None:
        if self.status.terminal:
            raise RuntimeError("Cannot append to a terminal Session")
        _validate_message(message)
        self.transcript.append(copy.deepcopy(message))

    def replace_transcript(self, messages: Iterable[dict]) -> None:
        if self.status.terminal:
            raise RuntimeError("Cannot replace a terminal Session transcript")
        replacement = copy.deepcopy(list(messages))
        for message in replacement:
            _validate_message(message)
        self.transcript[:] = replacement

    def checkpoint(self, status: SessionStatus) -> None:
        if status.terminal:
            raise ValueError("checkpoint requires a non-terminal status")
        self.status = status

    def finish(self, status: SessionStatus) -> None:
        if self.status.terminal:
            raise RuntimeError("Session is already terminal")
        if not status.terminal:
            raise ValueError("finish requires a terminal status")
        self.status = status

    def snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(
            identity=self.identity,
            workspace=self.workspace,
            transcript=tuple(copy.deepcopy(self.transcript)),
            status=self.status,
            metadata=copy.deepcopy(self.metadata),
            usage=self.usage,
        )
```

Validate IDs, roles/content, workspace, terminal transitions, and deep-copy snapshots. Do not introduce rich message subclasses in this task; existing dictionaries remain the compatibility wire model.

- [x] **Step 4: Run model tests**

Run: `pytest -q tests/runtime/session/test_model.py`

Expected: PASS.

- [x] **Step 5: Run style and no-commit checkpoint**

Run: `ruff check nz_coder/runtime/session tests/runtime/session/test_model.py && git diff --check -- nz_coder/runtime/session tests/runtime/session/test_model.py`

Expected: both checks pass.

### Task 3: Add SessionStore and the legacy JSON adapter

**Files:**
- Create: `nz_coder/runtime/session/store.py`
- Create: `tests/runtime/session/test_store.py`
- Modify: `nz_coder/runtime/session/__init__.py`
- Modify: `nz_coder/runtime/session_repository.py`

**Interfaces:**
- Consumes: `Session`, `SessionSnapshot`, existing `load_session()`/`save_session()`, `scoped_workdir()`, and `scoped_session()`.
- Produces: runtime-checkable `SessionStore`, `LegacyJsonSessionStore.load(identity, workspace) -> Session | None`, and `LegacyJsonSessionStore.save(session) -> None`.

- [x] **Step 1: Write failing store tests**

```python
def test_store_round_trip_preserves_parent_metadata_usage_and_parts(tmp_path, monkeypatch):
    store = LegacyJsonSessionStore()
    session = Session.create(
        "child-1",
        [{"role": "assistant", "content": "done", "_parts": []}],
        workspace=tmp_path,
        parent_session_id="parent-1",
        metadata={"permission_mode": "default"},
    )
    asyncio.run(store.save(session))
    restored = asyncio.run(store.load(session.identity, tmp_path))
    assert restored is not None
    assert restored.identity.parent_session_id == "parent-1"
    assert restored.transcript == session.transcript

def test_store_returns_none_for_missing_session(tmp_path):
    identity = SessionIdentity("missing")
    assert asyncio.run(LegacyJsonSessionStore().load(identity, tmp_path)) is None
```

- [x] **Step 2: Run tests and verify failure**

Run: `pytest -q tests/runtime/session/test_store.py`

Expected: FAIL because `SessionStore` and `LegacyJsonSessionStore` do not exist.

- [x] **Step 3: Implement the store Protocol and adapter**

```python
@runtime_checkable
class SessionStore(Protocol):
    async def load(self, identity: SessionIdentity, workspace: Path) -> Session | None:
        raise NotImplementedError

    async def save(self, session: Session) -> None:
        raise NotImplementedError
```

The adapter performs synchronous JSON operations through `asyncio.to_thread`, scopes workspace/session ContextVars, maps current payload metadata onto Session, and delegates atomic persistence to the existing `save_session()` implementation.

- [x] **Step 4: Convert FileSessionRepository into a compatibility adapter**

Keep its public name and current tests, but delegate `load`, `save`, and `checkpoint` to `LegacyJsonSessionStore` or the Phase 2A SessionRuntime. Do not retain a second copy of payload mapping logic.

- [x] **Step 5: Run focused compatibility tests**

Run: `pytest -q tests/runtime/session/test_store.py tests/runtime/test_session_repository.py tests/test_session_lifecycle.py tests/test_session_stats.py`

Expected: PASS with the current disk format unchanged.

### Task 4: Introduce production RunContext and SessionRuntime

**Files:**
- Create: `nz_coder/runtime/core/run_context.py`
- Create: `nz_coder/runtime/session/runtime.py`
- Create: `tests/runtime/session/test_session_runtime.py`
- Modify: `nz_coder/runtime/core/__init__.py`
- Modify: `nz_coder/runtime/session/__init__.py`

**Interfaces:**
- Consumes: `RunRequest`, `RunProfile`, `AgentDefinition`, `Session`, `SessionStore`, and `TokenUsage`.
- Produces: `RunContext`, `SessionRuntime.open(request)`, `SessionRuntime.checkpoint(context, status)`, and `SessionRuntime.finalize(context, status)`.

- [x] **Step 1: Write failing RunContext tests**

```python
def test_open_prefers_durable_session_and_appends_only_new_request_tail():
    store = MemorySessionStore(existing=Session.create(
        "s1", [{"role": "user", "content": "old"}], workspace=tmp_path,
    ))
    request = make_request(tmp_path, messages=[
        {"role": "user", "content": "old"},
        {"role": "user", "content": "new"},
    ])
    context = asyncio.run(SessionRuntime(store).open(request))
    assert [m["content"] for m in context.session.transcript] == ["old", "new"]

def test_finalize_persists_terminal_status_once():
    context = asyncio.run(runtime.open(request))
    asyncio.run(runtime.finalize(context, RunStatus.COMPLETED))
    with pytest.raises(RuntimeError, match="terminal"):
        asyncio.run(runtime.finalize(context, RunStatus.ERROR))
```

- [x] **Step 2: Run tests and verify failure**

Run: `pytest -q tests/runtime/session/test_session_runtime.py`

Expected: FAIL because `RunContext` and `SessionRuntime` do not exist.

- [x] **Step 3: Implement RunContext**

```python
@dataclass
class RunContext:
    request: RunRequest
    session: Session
    active_agent: str
    turn_count: int = 0
    iteration_count: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)
    retry_count: int = 0
    compaction_attempts: int = 0
    cancellation: object | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def transcript(self) -> list[dict]:
        return self.session.transcript
```

Move the useful behavior from `RunState` into this type. Keep `RunState` as a deprecated import alias or compatibility subclass only while current tests/consumers remain.

- [x] **Step 4: Implement SessionRuntime open/checkpoint/finalize**

`open()` loads the durable Session when present, otherwise creates one from the immutable request. It reconciles only a common-prefix-compatible request tail, preventing duplicated resume messages. `checkpoint()` persists a non-terminal snapshot. `finalize()` enforces exactly one terminal transition and persists it.

- [x] **Step 5: Run focused tests**

Run: `pytest -q tests/runtime/session/test_session_runtime.py tests/runtime/core/test_models.py`

Expected: PASS.

### Task 5: Wire SessionRuntime into the real AgentRunner path

**Files:**
- Modify: `nz_coder/runtime/core/contracts.py`
- Modify: `nz_coder/runtime/services.py`
- Modify: `nz_coder/runtime/runner.py`
- Modify: `nz_coder/runtime/composition.py`
- Modify: `tests/runtime/test_runner.py`
- Create: `tests/runtime/session/test_runner_integration.py`

**Interfaces:**
- Consumes: `RunContext`, `SessionRuntime`, and the existing `AgentLoop` compatibility host.
- Produces: one production Runner invocation that opens a Session before lifecycle initialization and persists the same Session-owned transcript at every checkpoint.

- [x] **Step 1: Write a failing Runner integration test**

```python
def test_production_runner_opens_one_run_context_and_uses_session_transcript():
    runtime = RecordingSessionRuntime()
    services = make_services(session_runtime=runtime)
    host = SettledHost()
    messages = [{"role": "user", "content": "work"}]
    asyncio.run(AgentRunner(services).run(host, messages, stream=False))
    assert runtime.opened == ["runner-session"]
    assert runtime.checkpoints == [("runner-session", "running")]
    assert host.active_run_context is None
```

- [x] **Step 2: Run the integration test and verify failure**

Run: `pytest -q tests/runtime/session/test_runner_integration.py`

Expected: FAIL because AgentRunner does not open a Session/RunContext.

- [x] **Step 3: Add the SessionRuntime service contract**

```python
class SessionRuntimePort(Protocol):
    async def open(self, request: RunRequest) -> RunContext:
        raise NotImplementedError

    async def checkpoint(self, context: RunContext, status: str) -> None:
        raise NotImplementedError

    async def finalize(self, context: RunContext, status: RunStatus) -> None:
        raise NotImplementedError
```

Add it to `RuntimeServices`. Keep a compatibility `sessions` property if
necessary, but only one concrete object may own production persistence.

- [x] **Step 4: Add legacy-host request projection at the composition boundary**

Construct one `RunRequest` from the host's declared Agent/profile/session/workspace/tool policy. This projection belongs in the compatibility adapter or SessionRuntime, not in the core Runner loop.

- [x] **Step 5: Run the existing loop against `context.transcript`**

`AgentRunner.run()` opens a RunContext, exposes it to the temporary host adapter for un-migrated helpers, and passes `context.transcript` to `_run_turns`. Mirror settled mutations back into the caller-owned list before returning so the public facade remains compatible. Clear active context in `finally`.

- [x] **Step 6: Route checkpoints through SessionRuntime**

Replace direct `services.sessions.checkpoint(host, messages, status)` sites in Runner with awaited SessionRuntime checkpoints. Terminal persistence remains coordinated with `ProductionRunLifecycle` during this phase; add an exactly-once guard so both layers cannot finalize independently.

- [x] **Step 7: Run focused production-chain tests**

Run: `pytest -q tests/runtime/test_runner.py tests/runtime/session/test_runner_integration.py tests/runtime/test_sdk.py tests/test_runtime_composition.py`

Expected: PASS and the integration test proves the production Runner created a RunContext.

### Task 6: Make SessionProcessor report all stable message mutations

**Files:**
- Modify: `nz_coder/runtime/session_processor.py`
- Create: `tests/runtime/session/test_processor_sink.py`
- Modify: `nz_coder/runtime/runner.py`

**Interfaces:**
- Consumes: existing assistant dictionaries and Session-owned transcript.
- Produces: optional `on_message_updated(message: dict) -> None` callback invoked after every stable part/content/error/finish transition.

- [x] **Step 1: Write a failing sink-order test**

```python
def test_processor_sink_observes_text_tool_and_finish_in_order():
    observed = []
    processor = SessionProcessor(message(), on_message_updated=lambda msg: observed.append(copy.deepcopy(msg)))
    processor.stream_text("hello", part_id="text-1")
    processor.register_tool_calls([tool_call("call-1")])
    processor.finish_step("tool-calls")
    assert [snapshot["_parts"][-1]["type"] for snapshot in observed] == [
        "text", "tool", "step-finish",
    ]
```

- [x] **Step 2: Run test and verify constructor failure**

Run: `pytest -q tests/runtime/session/test_processor_sink.py`

Expected: FAIL because `on_message_updated` is not accepted.

- [x] **Step 3: Implement the sink at `_update` and direct content/error mutations**

Keep the existing lock and publish behavior. Invoke the sink with the current assistant message only after a stable mutation is committed. Do not call it for transient parser buffers.

- [x] **Step 4: Bind the sink to Session ownership**

The sink marks the owning Session dirty. It intentionally does not persist on
every streaming mutation; explicit Runner lifecycle boundaries checkpoint via
`SessionRuntime`.

Runner-created processors receive a sink that marks the Session dirty and emits the existing session event. Persistence stays at settled boundaries; the sink must not perform blocking disk I/O per token.

- [x] **Step 5: Run processor and message-schema tests**

Run: `pytest -q tests/runtime/session/test_processor_sink.py tests/test_session_processor.py tests/test_message_schema.py`

Expected: PASS with unchanged message dictionaries and event order.

### Task 7: Verify Phase 2A and update architecture records

**Files:**
- Modify: `docs/infcode-alignment-learning-log.md`
- Modify: `docs/superpowers/plans/2026-08-10-session-first-runtime-phase-2a.md`

**Interfaces:**
- Consumes: Tasks 1-6.
- Produces: verified Phase 2A evidence and an explicit list of remaining Phase 2B work.

- [x] **Step 1: Run all new Session/Runner tests**

Run: `pytest -q tests/runtime/session tests/runtime/core tests/runtime/test_runner.py tests/runtime/test_session_repository.py tests/runtime/test_sdk.py`

Expected: PASS.

- [x] **Step 2: Run architecture and dependency tests**

Run: `pytest -q tests/runtime/model_gateway tests/runtime/tool_runtime tests/runtime/test_context_architecture.py tests/test_architecture_boundary.py`

Expected: PASS.

- [x] **Step 3: Run full validation**

Run: `pytest -q`

Expected: all tests pass.

Run: `ruff check nz_coder tests`

Expected: `All checks passed!`

Run: `python -m compileall -q nz_coder`

Expected: exit code 0.

- [x] **Step 4: Run provider-free runtime smoke**

Run: `python -m nz_coder.evaluation.parallel_benchmark --tasks 6 --delay 0.02 --parallel-limit 3 --json`

Expected: exit code 0, ordered results, and peak concurrency three.

- [x] **Step 5: Re-run import-cycle and private-host access audits**

Record the SCC count and `host._*` call count. Phase 2A is expected to wire Session/RunContext, not eliminate every private-host call. Any unchanged debt must remain explicit.

- [x] **Step 6: Update the learning log truthfully**

Record exact tests and source-level ownership achieved. Mark host-free Model/Tool/Context ports, Main facade removal, child/background Session cutover, global ToolRegistry migration, and compatibility deletion as remaining work unless their production consumers are actually gone.

- [x] **Step 7: Final no-commit checkpoint**

Run: `git diff --check`

Expected: no whitespace errors. Do not commit the mixed worktree.

## Phase 2A completion boundary

Phase 2A is complete only when the production Runner constructs one RunContext,
the RunContext owns one Session, every Runner checkpoint persists that Session,
the public caller-list behavior remains compatible, and the full test suite
passes. It does not claim that AgentLoop has been eliminated or that SubAgent is
already a native child Session; those are subsequent Phase 2B/2C plans.
