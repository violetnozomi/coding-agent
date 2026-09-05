# Real coding and terminal acceptance R1

> Execution: inline, using the executing-plans workflow; this is a bounded
> acceptance exercise, not a benchmark framework or a new runtime design.

**Goal:** Observe four coding requests and four interaction scenarios through
installed NZ-Coder product entrypoints, with independent acceptance.

**Baseline:** `89124f9870e61a38290b1af1c97b0529da7188bb` (origin/main).
**Branch:** `codex/dogfood-product-r1`; do not modify PR #2 or merge this branch.
**Stack:** Python standard library fixtures, existing NZ-Coder wheel, pytest,
Linux PTY, real loopback daemon/HTTP/attach. No new product dependency.

## Frozen scope and accounting

- Four first attempts: T01 headless persistence; T02 local terminal status;
  T03 limit in the same live T02 session; T04 daemon/remote log grouping.
- At most three affected-task retests and one diagnostic attempt; at most eight
  real coding attempts, sequential. Preserve every outcome, including failures.
- Four deterministic interaction scenarios: F01 permission; F02 cancellation;
  F03 one disconnect/reconnect; F04 bounded long output and terminal resize.
  These do not count as model coding successes.
- At most three confirmed A/B product fixes, only after freezing first runs.
- Requests, baseline commits, visible tests, allowed paths and independent
  expectations are frozen by `fixtures.py` and `accept.py` before model runs.
- Executor never implements a requested fixture solution. Independent verifier
  stays outside task workspaces. This is not an OS sandbox.
- No real secrets, raw reasoning, host environment dumps, absolute personal paths,
  generated projects, build products or venvs are committed.
- Reports live here because this repository deliberately excludes most docs and
  scripts. `docs/struct.md` receives a short link and product conclusion.

## Provider and limits

Use the user's existing DeepSeek endpoint and deepseek-v4-flash. The installed
test-only `r1-metered` adapter delegates to the product's OpenAI-compatible
adapter; it does not generate answers or intercept tool execution. It disables
SDK retries and reserves an upper charge before each request against a shared
USD 5 ledger, including all auxiliary requests. No refund of unused reservations.
Model rounds 12, tool calls per response 3, output tokens 4096, context 65536,
provider deadline 90 seconds, outer task deadline 600 seconds, no model fallback.
Planning/reflection/LLM memory extraction/reranking disabled; subagent tools denied
by restrictive fixture settings. Product trust and endpoint checks stay enabled.

Official price source (checked 2026-09-05):
https://api-docs.deepseek.com/quick_start/pricing/?article_id=article_1779470751466_8
Use peak/cache-miss USD 0.44 input and USD 1.32 output per million tokens.
Reservation uses serialized UTF-8 request bytes plus 4096 framing tokens as an
input upper bound, and the explicit output cap. Validate supported text-only
request/model/endpoint; reject unknown costs or excess budget before network I/O.
Provider-reported usage and conservative reserved cost are separate fields;
invoices are not inferred. If the gate cannot be established, mark T01–T04 NOT_RUN.

## Execution checklist

- [x] Read spec; check clean root, fetch main, verify accepted merge ancestry.
- [x] Create independent worktree; run baseline tests; build and install wheel.
- [x] Prepare two minimal multi-file projects and external verifier. Initialize
  isolated fixture Git repos; freeze task text, allowed paths and baseline SHAs.
  Run visible tests and independent verifier before NZ-Coder; each target must fail.
- [x] Verify the metering adapter rejects a request before dispatch when its
  bounded reservation exceeds the ledger. Verify installed core module sources.
- [x] Attempt T01 launch with `python -m nz_coder run --max-turns 12 --output jsonl`:
  exit 3 before Provider dispatch, missing credentials. Actual coding NOT_RUN.
- [x] Record T02/T03 NOT_RUN under the no-credential stop condition. Same-session
  real-model editing is NOT_VERIFIED, not replaced with scripted coding.
- [x] Record T04 NOT_RUN under the same stop condition. Intended entry: daemon/attach;
  no fallback is counted as coding success. Original plan: fallback only to formal SDK
  if remote PTY is unavailable, marking visual acceptance NOT_VERIFIED.
- [x] Drive F01–F04 through formal product entrypoints with deterministic
  provider transport. Save state/event evidence and bounded terminal transcripts.
  Do not call manually injected renderer events product validation.
- [x] Freeze first-run table and classify A/B/C/D issues before any product edit.
- [x] Reproduce one A defect with two failing regressions,
  minimally fix, reinstall wheel and repeat the affected unchanged scenario.
- [ ] Run diff/compile/Ruff/pytest/build and outside-source final install checks.
- [ ] Record real task/scene results, separate timing and cost/usage fields,
  retry ledger, visual limitations and one next recommendation. Commit necessary
  artifacts only, ordinary push and create a new PR; inspect final-head CI.

## Frozen independent conditions

T01: missing/empty storage yields []; corrupt nonempty JSON yields exit 2 and a
clear stderr error, bytes unchanged even for add; normal data and visible tests pass.
T02: list --status open/done/all filters done booleans; default all unchanged;
empty match []; invalid status exit 2; visible tests pass; add tests and README.
T03: list --limit N accepts nonnegative integers; applies after status; zero [];
negative/nonnumeric invalid; status remains correct; no new application modules.
T04: JSONL --group-by level/module; default level; sorted JSON object of counts;
empty {}; invalid/missing-field line rejects entire request with line number,
exit 2 and no partial stdout; invalid group exit 2; visible tests pass.

Allowed solution paths: todo/*.py, tests/*.py, README.md for T01–T03; logstats/*.py,
tests/*.py, README.md for T04. No generated data or control-plane edits permitted.
