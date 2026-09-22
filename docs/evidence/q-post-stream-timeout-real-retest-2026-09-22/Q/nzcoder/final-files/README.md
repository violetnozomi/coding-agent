# Delivery pool

Bounded-concurrency helpers exported from `index.cjs`: `mapLimit` (from
`lib/pool.cjs`) and `processBatch` (from `lib/batch.cjs`). Node built-ins only.

## `mapLimit(items, limit, worker, options?)`

Returns a `Promise` of results in **input order**.

- `items`: array (never mutated).
- `limit`: positive integer, maximum workers in flight at any moment.
- `worker(item, index, signal)`: may be sync or async, may throw synchronously.
- `options.signal`: optional `AbortSignal` passed through to the worker.

### Concurrency

Workers are started as soon as a slot frees up — the pool does not wait for a
whole batch to finish before starting the next item, and each item is started
exactly once. The number of in-flight workers never exceeds `limit`.

### Ordering and falsy results

Results are placed at their input index, so completion order does not matter.
Falsy results (`0`, `''`, `false`, `null`, `undefined`, `NaN`) are recorded as-is
and are never overwritten.

### Validation

Invalid arguments reject with a `TypeError` **before any worker runs**, including
for empty input: non-array `items`, non-positive/non-integer `limit`, or a
non-callable `worker`.

### Failure draining

Once a worker rejects (or throws synchronously), no new work is started. All
already-started workers are allowed to settle, and then the promise rejects with
the **first observed** worker error, preserving object identity. Rejection
handlers are attached immediately, so secondary failures never surface as
unhandled rejections.

### Cancellation

- If `signal` is already aborted, no work starts and the promise rejects with
  `signal.reason` (identity preserved, including non-Error reasons). This also
  takes precedence over the empty-input rule.
- If abort arrives mid-run, scheduling stops, in-flight workers are drained, and
  the promise rejects with the abort reason that was observed first.
- In-flight work is *not* force-stopped: an uncooperative worker is waited on
  rather than pretended to be cancelled. No delays or polling are used as
  correctness machinery.
- Abort listeners installed by `mapLimit` are removed after settlement.
- Empty input resolves `[]` when not aborted.

## `processBatch(records, send, options?)`

Maps `records` through `send(record, index, signal)` with bounded concurrency and
returns `{id, value}` records in input order.

- `options.concurrency`: default `2`; must be a positive integer.
- `options.signal`: forwarded to `mapLimit`, enabling the cancellation semantics
  above.
- Each record is sent **at most once**, and a rejection (worker or abort) is
  propagated unchanged.
