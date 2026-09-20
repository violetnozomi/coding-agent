'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { mapLimit } = require('../index.cjs');

const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

// A minimal AbortSignal stand-in so tests can assert listener cleanup.
function makeFakeSignal() {
  const listeners = new Set();
  return {
    aborted: false,
    reason: undefined,
    addEventListener(_type, fn) {
      listeners.add(fn);
    },
    removeEventListener(_type, fn) {
      listeners.delete(fn);
    },
    listenerCount() {
      return listeners.size;
    },
    abort(reason) {
      this.aborted = true;
      this.reason = reason;
      for (const fn of [...listeners]) fn();
    },
  };
}

// --- basics ---------------------------------------------------------------

test('maps values and preserves falsy results', async () => {
  assert.deepEqual(await mapLimit([1, 2], 2, (x) => x - 1), [0, 1]);
});

test('empty input resolves [] when not aborted', async () => {
  const controller = new AbortController();
  assert.deepEqual(await mapLimit([], 3, () => 1, { signal: controller.signal }), []);
});

test('preserves every falsy worker result verbatim', async () => {
  const falsy = [0, '', false, null, undefined];
  const out = await mapLimit([0, 1, 2, 3, 4], 2, (x) => falsy[x]);
  assert.equal(out.length, 5);
  assert.deepEqual(out, falsy);
});

test('worker receives (item, index, signal)', async () => {
  const controller = new AbortController();
  const seen = [];
  await mapLimit(['a', 'b'], 1, (item, index, signal) => {
    seen.push([item, index, signal === controller.signal]);
    return item;
  }, { signal: controller.signal });
  assert.deepEqual(seen, [['a', 0, true], ['b', 1, true]]);
});

// --- validation -----------------------------------------------------------

test('rejects a non-array items argument with TypeError', async () => {
  await assert.rejects(mapLimit('nope', 1, (x) => x), TypeError);
  await assert.rejects(mapLimit(null, 1, (x) => x), TypeError);
});

test('rejects a non-positive / non-integer limit with TypeError', async () => {
  for (const limit of [0, -1, 1.5, '2', NaN, Infinity]) {
    await assert.rejects(mapLimit([1], limit, (x) => x), TypeError);
  }
});

test('rejects a non-callable worker with TypeError, even for empty input', async () => {
  await assert.rejects(mapLimit([], 2, null), TypeError);
  await assert.rejects(mapLimit([1, 2], 2, 'x'), TypeError);
});

test('validates before invoking the worker', async () => {
  let calls = 0;
  await assert.rejects(
    mapLimit([1, 2, 3], 0, () => { calls++; }),
    TypeError,
  );
  assert.equal(calls, 0);
});

test('rejects a malformed signal with TypeError', async () => {
  await assert.rejects(mapLimit([1], 1, (x) => x, { signal: {} }), TypeError);
});

test('does not mutate the input array', async () => {
  const items = [3, 1, 2];
  const snapshot = items.slice();
  await mapLimit(items, 2, (x) => x * 2);
  assert.deepEqual(items, snapshot);
});

// --- ordering & concurrency ----------------------------------------------

test('preserves input order regardless of completion order', async () => {
  const out = await mapLimit([30, 5, 20, 0], 4, async (x) => {
    await tick(x);
    return x;
  });
  assert.deepEqual(out, [30, 5, 20, 0]);
});

test('never exceeds the limit and fills free slots eagerly', async () => {
  let active = 0;
  let maxActive = 0;
  const started = [];
  const out = await mapLimit([...Array(10).keys()], 3, async (x) => {
    started.push(x);
    active++;
    maxActive = Math.max(maxActive, active);
    await tick(5);
    active--;
    return x;
  });
  assert.equal(maxActive, 3);
  assert.deepEqual(started, [...Array(10).keys()]); // started in order, each once
  assert.deepEqual(out, [...Array(10).keys()]);
});

test('starts each item exactly once', async () => {
  const counts = new Map();
  await mapLimit([...Array(8).keys()], 3, (x) => {
    counts.set(x, (counts.get(x) || 0) + 1);
    return x;
  });
  for (let i = 0; i < 8; i++) assert.equal(counts.get(i), 1);
});

test('supports a limit larger than the item count', async () => {
  const out = await mapLimit([1, 2], 100, async (x) => x);
  assert.deepEqual(out, [1, 2]);
});

// --- failure draining -----------------------------------------------------

test('drains after the first failure, preserves its identity, and starts no more work', async () => {
  const boom = new Error('boom');
  const started = [];
  await assert.rejects(
    mapLimit([0, 1, 2, 3, 4], 2, async (x) => {
      started.push(x);
      if (x === 0) throw boom;
      await tick(10);
      return x;
    }),
    (err) => err === boom,
  );
  assert.deepEqual(started, [0, 1]); // 2..4 were never started
});

test('handles a synchronous worker throw and stops scheduling', async () => {
  const boom = new Error('sync boom');
  const started = [];
  await assert.rejects(
    mapLimit([0, 1, 2, 3], 2, (x) => {
      started.push(x);
      if (x === 0) throw boom;
      return x;
    }),
    (err) => err === boom,
  );
  assert.deepEqual(started, [0, 1]);
});

test('first observed worker error wins and secondary rejections stay handled', async () => {
  const first = new Error('first');
  const second = new Error('second');
  const unhandled = [];
  const onUnhandled = (reason) => unhandled.push(reason);
  process.on('unhandledRejection', onUnhandled);
  try {
    await assert.rejects(
      mapLimit([0, 1, 2], 3, async (x) => {
        if (x === 0) throw first;
        if (x === 1) throw second;
        return x;
      }),
      (err) => err === first,
    );
    await tick(20);
    assert.deepEqual(unhandled, []);
  } finally {
    process.off('unhandledRejection', onUnhandled);
  }
});

// --- cancellation ---------------------------------------------------------

test('already-aborted signal starts no work and rejects with the reason identity', async () => {
  const controller = new AbortController();
  const reason = new Error('cancelled');
  controller.abort(reason);
  let calls = 0;
  await assert.rejects(
    mapLimit([1, 2, 3], 2, () => { calls++; }, { signal: controller.signal }),
    (err) => err === reason,
  );
  assert.equal(calls, 0);
});

test('preserves a non-Error abort reason', async () => {
  const controller = new AbortController();
  controller.abort('nope');
  await assert.rejects(
    mapLimit([1, 2], 1, () => 1, { signal: controller.signal }),
    (err) => err === 'nope',
  );
});

test('abort mid-run stops scheduling, drains in-flight work, and rejects with the reason', async () => {
  const controller = new AbortController();
  const reason = new Error('stop');
  const started = [];
  let completed = 0;
  const promise = mapLimit([...Array(6).keys()], 2, async (x) => {
    started.push(x);
    await tick(10);
    completed++;
    return x;
  }, { signal: controller.signal });

  await tick(0);
  controller.abort(reason);

  await assert.rejects(promise, (err) => err === reason);
  assert.deepEqual(started, [0, 1]); // no new work scheduled after abort
  assert.equal(completed, 2); // in-flight work was allowed to settle
});

test('does not resolve before an uncooperative in-flight worker settles', async () => {
  const controller = new AbortController();
  const reason = new Error('cancel');
  let workerFinished = false;
  let rejected = false;
  const promise = mapLimit([1], 1, async () => {
    await tick(30);
    workerFinished = true;
    return 'done';
  }, { signal: controller.signal });
  promise.catch(() => { rejected = true; });

  controller.abort(reason);
  await tick(10);
  assert.equal(rejected, false);
  assert.equal(workerFinished, false);

  await assert.rejects(promise, (err) => err === reason);
  assert.equal(workerFinished, true);
});

test('abort observed before a worker error wins', async () => {
  const controller = new AbortController();
  const abortReason = new Error('abort-wins');
  const workerError = new Error('worker');
  const promise = mapLimit([1], 1, async () => {
    await tick(10);
    throw workerError;
  }, { signal: controller.signal });

  controller.abort(abortReason);
  await assert.rejects(promise, (err) => err === abortReason);
});

test('worker error observed before an abort wins', async () => {
  const controller = new AbortController();
  const workerError = new Error('worker-wins');
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const promise = mapLimit([1, 2], 2, async (x) => {
    if (x === 1) throw workerError; // index 0 fails immediately
    await gate; // index 1 stays in flight
    return x;
  }, { signal: controller.signal });

  await tick(5);
  controller.abort(new Error('late'));
  release();

  await assert.rejects(promise, (err) => err === workerError);
});

test('removes abort listeners after a successful settlement', async () => {
  const signal = makeFakeSignal();
  const out = await mapLimit([1, 2, 3], 2, (x) => x, { signal });
  assert.deepEqual(out, [1, 2, 3]);
  assert.equal(signal.listenerCount(), 0);
});

test('removes abort listeners after an abort rejection', async () => {
  const signal = makeFakeSignal();
  const reason = new Error('x');
  const promise = mapLimit([1, 2], 1, async () => { await tick(5); return 1; }, { signal });
  signal.abort(reason);
  await assert.rejects(promise, (err) => err === reason);
  assert.equal(signal.listenerCount(), 0);
});

test('removes abort listeners after a worker failure', async () => {
  const signal = makeFakeSignal();
  const boom = new Error('boom');
  await assert.rejects(
    mapLimit([1, 2], 1, () => { throw boom; }, { signal }),
    (err) => err === boom,
  );
  assert.equal(signal.listenerCount(), 0);
});
