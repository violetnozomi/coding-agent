# NZ-Coder Evaluation

NZ-Coder includes a benchmark harness for testing coding-agent behavior without relying on anecdotal demos.

## Running

```bash
python -m nz_coder.benchmark --list
python -m nz_coder.benchmark
python -m nz_coder.benchmark --report
```

Outputs:

- `.nz-coder/benchmark/report.json`
- `.nz-coder/benchmark/report.md`
- `.nz-coder/benchmark/runs/*.jsonl`

## Task Coverage

The benchmark currently covers:

- file creation
- bug fixing
- feature addition
- test authoring
- test repair
- multi-file debugging
- CLI behavior changes
- structured JSON edits
- refactoring while preserving public API
- documentation updates

## Metrics

Each task records:

- pass/fail and verification reason
- task type and difficulty
- duration
- assistant turns
- tool calls and tool errors
- trace path

The report aggregates pass rate, average turns/tools/time, pass rate by difficulty, pass rate by task type, and failure categories.

## Unit-Level Runtime Tests

`tests/test_loop_fake.py` uses a fake OpenAI-compatible client to test the agent loop without calling a real model. It verifies:

- tool-call execution followed by final response
- invalid tool JSON feedback
- transient API retry
- trace event creation

