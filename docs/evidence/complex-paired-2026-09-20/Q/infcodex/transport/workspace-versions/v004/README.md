# Delivery pool

A tiny, dependency-free concurrency helper for Node. It runs a worker over a
list of items with a bounded number of jobs in flight, and a batch helper built
on top of it for "send each record once" workloads.

Exports:

- `mapLimit(items, limit, worker, options?)` — from `lib/pool.cjs`
- `processBatch(records, send, options?)` — from `lib/batch.cjs`

Both are re-exported from the package entry point (`index.cjs`).

## `mapLimit(items, limit, worker, { signal }?)`

Returns a `Promise` of results **in input order**.

- `items` — an array (never mutated).
- `limit` — a positive integer concurrency cap.
- `worker(item, index, signal)` — called once per item; may return a value or a
  `Promise`, and may `throw` synchronously.
- `options.signal` — optional `AbortSignal` for cancellation.

### Validation

Invalid arguments **reject with a `TypeError` before any worker is invoked**,
and validation happens even for empty input:

- `items` not an array → `TypeError`
- `limit` not a positive integer (`0`, `-1`, `1.5`, `NaN`, `Infinity`, `'2'`) → `TypeError`
- `worker` not callable → `TypeError`
- `options.signal` present but not an `AbortSignal` → `TypeError`

### Concurrency

At most `limit` workers run at any moment. Free slots are filled **eagerly** —
the next item starts as soon as a slot frees, without waiting for the whole
batch. Every item is started **exactly once**.

### Result ordering

Results are written to their input position as workers settle, so the resolved
array always matches the input order regardless of completion order. Falsy
worker results (`0`, `''`, `false`, `null`, `undefined`) are stored verbatim —
they are never replaced by a fallback.

### Failure draining

The **first observed** worker failure (including a synchronous `throw`) stops
scheduling: no new items are started. Already-started workers are allowed to
**settle** — the pool never pretends an uncooperative worker has stopped — and
then the promise rejects with the first failure, preserving its object identity.
Rejection handlers are attached to every worker immediately, so secondary
failures never surface as unhandled rejections.

### Cancellation

`options.signal` is an `AbortSignal`:

- **Already aborted** — no work starts; the promise rejects with
  `signal.reason`, preserving identity (including non-`Error` reasons such as a
  string).
- **Aborted mid-run** — scheduling stops, in-flight workers are drained, then
  the promise rejects with the abort reason.
- The **first observed failure or abort wins** (a worker error observed before
  an abort is reported, and vice versa).
- The abort listener is **removed once the promise settles** (resolve or
  reject), so repeated use does not leak listeners.

The `signal` is also forwarded to the worker, so cooperative workers can cancel
their own I/O.

### Example

```js
const { mapLimit } = require('./index.cjs');

const results = await mapLimit([1, 2, 3, 4, 5], 2, async (n, index, signal) => {
  return n * n;
});
// -> [1, 4, 9, 16, 25]  (input order, at most 2 workers at a time)
```

## `processBatch(records, send, { concurrency = 2, signal }?)`

Calls `mapLimit` with `options.concurrency` (default `2`) and `options.signal`,
and returns ordered `{ id, value }` records:

```js
const { processBatch } = require('./index.cjs');

const out = await processBatch(
  [{ id: 'a' }, { id: 'b' }],
  async (record, signal) => sendOverNetwork(record),
  { concurrency: 4 },
);
// -> [{ id: 'a', value: ... }, { id: 'b', value: ... }]
```

Semantics:

- **Ordered** — output order matches input order.
- **At most once** — each record's `send` is invoked exactly once.
- **Bounded concurrency** — `options.concurrency` (default `2`), validated by
  `mapLimit` (`TypeError` on invalid values).
- **Propagates rejection** — a `send` failure drains in-flight sends and rejects
  with the same error object, starting no further records.
- **Cancellation** — `options.signal` stops scheduling and drains in-flight
  sends, then rejects with `signal.reason`. The signal is passed to `send` as
  its second argument for cooperative cancellation.

## Tests

Node built-ins only — no dependency install, no network:

```sh
node --test tests/pool.test.cjs tests/batch.test.cjs
# or
npm test
```
