# Contributing

Use Python 3.10 or newer. The CI runtime job uses Python 3.12 and Node 24; the
compatibility job also exercises the declared Python 3.10 floor. Install the
project and development dependencies in your own environment:

```bash
python -m pip install -e '.[dev]' build
```

Run the complete suite with the CI runtime versions (Python 3.12 and Node 24).
The Python 3.10 job runs the focused contracts listed in `core-runtime.yml`.
Before submitting a change:

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

The historical private HTTP smoke driver and v1 working-note migration script
are outside the supported test suite. Product coverage stays at the public
entry points: `test_http_service.py` covers event replay and late approval
replies; `test_http_verification_scope.py` covers live batch approvals and
persisted verified results; `test_http_result_recovery.py` covers terminal
records and interrupted recovery. `test_scratchpad_model_owned.py` checks that
unsupported older note files are rejected without overwriting them.
The former SWE-bench Pro entry checks depended on a historical local driver.
The tracked Verified and Lite entry is tested in `test_swebench_protocol.py`;
`test_swebench_strict.py` and `test_focused_policy.py` check public command and
workspace boundaries. The prior Pro prompt rewrite and Go admission are
specific to the historical experiment.
Private budget or shutdown controllers under historical evidence directories
require their original experiment artifacts. The tracked review driver and
relay are covered by `test_review_stop_cleanup.py` and `test_model_relay.py`.

The public `AgentClient` normally runs the production runner. If you supply an
explicit `agent_factory`, the custom agent's `run` result must provide its final
text in `content` and its terminal state in `status`. The SDK does not infer a
new answer from an old assistant message in the transcript. A result without
independent verification may be `completed_unverified`.

A persisted session has one active writer from loading through final storage.
Concurrent clients targeting the same session receive `session_busy`; different
session identities can run independently. Failed final storage retains the
original result for `retry_settlement`, which does not run the model again.
