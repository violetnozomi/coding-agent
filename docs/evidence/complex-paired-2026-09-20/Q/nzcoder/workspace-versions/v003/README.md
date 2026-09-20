# Delivery pool

Node-only helpers for running work with **bounded concurrency**:

- `mapLimit(items, limit, worker, {signal} = {})` (from `lib/pool.cjs`)
- `processBatch(records, send, {concurrency = 2, signal} = {})` (from `lib/batch.cjs`)

Both are re-exported from `index.cjs`.

```js
const {mapLimit, processBatch} = require('./index.cjs');

// at most 3 workers in flight, results in input order
const doubled = await mapLimit([1, 2, 3, 4], 3, async (n, index, signal) => n * 2);

// every record is sent at most once; resolves to [{id, value}] in input order
const sent = await processBatch(
  [{id: 'a'}, {id: 'b'}],
  async (record, index, signal) => deliver(record),
  {concurrency: 4, signal: controller.signal}
);
```

## `mapLimit(items, limit, worker, options)`

- `items` must be an array, `limit` a positive integer, `worker` a function.
  Invalid arguments reject with `TypeError` **before** `worker` runs, including
  for empty input. `items` is never mutated.
- `worker(item, index, signal)` may return a value, return a promise, or throw
  synchronously. Results resolve in **input order**, and falsy results are kept.
- `mapLimit([], limit, worker)` resolves to `[]` when not aborted.

### Concurrency

At most `limit` workers are ever in flight. Free slots are refilled as soon as
any worker settles, so a slow item never blocks the rest of the batch. Each item
starts exactly once.

### Failure draining

When a worker failure is observed (rejection or synchronous throw) no new item
is started, but already-started workers are **drained**: the returned promise
waits for them to settle and then rejects with the **first observed** error
(identity preserved). Rejection handlers are attached to every started worker,
so secondary failures never become unhandled rejections.

### Cancellation

Pass an `AbortSignal` as `options.signal`:

- already aborted → no work starts, the promise rejects with `signal.reason`
  (identity preserved, non-`Error` reasons included), even for empty input;
- aborted mid-run → scheduling stops, in-flight workers are drained, then the
  promise rejects with `signal.reason`; the first observed outcome (failure or
  abort) wins.

A cooperative worker can watch the same signal it is given. Uncooperative
workers are **not** force-stopped: the promise stays pending until they settle
(no timers, polling or arbitrary delays are used). The installed `abort`
listener is removed once the promise settles.

## `processBatch(records, send, options)`

- Calls `mapLimit` with `options.concurrency` (default `2`) and `options.signal`.
- `send(record, index, signal)` is invoked at most once per record and receives
  the same signal, so cooperative senders can stop early.
- Resolves to ordered `{id, value}` records (`id` from `record.id`); the first
  rejection, or the abort reason, is propagated unchanged.

## Verification

```sh
node --test tests/pool.test.cjs tests/batch.test.cjs
```
