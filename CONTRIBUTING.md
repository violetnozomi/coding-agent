# Contributing

Use Python 3.10 or newer. The CI runtime job uses Python 3.12 and Node 24; the
compatibility job also exercises the declared Python 3.10 floor. Install the
project and development dependencies in your own environment:

```bash
python -m pip install -e '.[dev]' build
```

Run the checks declared in CI before submitting a change:

```bash
python -m compileall -q nz_coder
python -m pytest -q
ruff check .
python -m build --wheel --sdist
```

Tests should use temporary workspaces and controlled providers. They must not
need a real API key or make paid model calls. Keep assertions about actual file
changes, imported code, process exit, persisted history, and verification facts.
A passing command alone does not prove delivery review succeeded.

When reporting a failure, include the command, Python/Node versions, traceback,
and a small reproduction. Remove credentials and private source from logs.
Distinguish a failed test from an unavailable external prerequisite or a missing
historical evaluation script; do not skip assertions to make the suite pass.

The public `AgentClient` normally runs the production runner. If you supply an
explicit `agent_factory`, the custom agent's `run` result must provide its final
text in `content` and its terminal state in `status`. The SDK does not infer a
new answer from an old assistant message in the transcript. A result without
independent verification may be `completed_unverified`.

A persisted session has one active writer from loading through final storage.
Concurrent clients targeting the same session receive `session_busy`; different
session identities can run independently. Failed final storage retains the
original result for `retry_settlement`, which does not run the model again.
