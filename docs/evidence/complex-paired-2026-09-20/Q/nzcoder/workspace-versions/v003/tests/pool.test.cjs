'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {mapLimit} = require('../index.cjs');

const tick = () => new Promise(resolve => setImmediate(resolve));

test('maps values and preserves falsy results', async () => {
  assert.deepEqual(await mapLimit([1, 2], 2, x => x - 1), [0, 1]);
});

test('empty input resolves []', async () => {
  assert.deepEqual(await mapLimit([], 2, x => x), []);
});

test('preserves input order regardless of completion order', async () => {
  const delays = {a: 30, b: 0, c: 15, d: 5};
  const results = await mapLimit(['a', 'b', 'c', 'd'], 4, async item => {
    await new Promise(resolve => setTimeout(resolve, delays[item]));
    return item.toUpperCase();
  });
  assert.deepEqual(results, ['A', 'B', 'C', 'D']);
});

test('worker receives item, index and signal', async () => {
  const seen = [];
  const controller = new AbortController();
  await mapLimit(['x', 'y'], 1, (item, index, signal) => {
    seen.push([item, index, signal]);
    return item;
  }, {signal: controller.signal});
  assert.deepEqual(seen.map(([item, index]) => [item, index]), [['x', 0], ['y', 1]]);
  assert.equal(seen[0][2], controller.signal);
});

test('never exceeds limit in flight', async () => {
  let inFlight = 0;
  let peak = 0;
  const results = await mapLimit([0, 1, 2, 3, 4, 5], 2, async item => {
    inFlight += 1;
    peak = Math.max(peak, inFlight);
    await tick();
    inFlight -= 1;
    return item * 10;
  });
  assert.equal(peak, 2);
  assert.deepEqual(results, [0, 10, 20, 30, 40, 50]);
});

test('refills a free slot without waiting for the whole batch', async () => {
  const started = [];
  const resolvers = [];
  const promise = mapLimit([0, 1, 2], 2, item => {
    started.push(item);
    return new Promise(resolve => {
      resolvers[item] = () => resolve(item);
    });
  });
  await tick();
  assert.deepEqual(started, [0, 1]);

  resolvers[0]();
  await tick();
  // Item 0's slot is reused for item 2 while item 1 is still in flight.
  assert.deepEqual(started, [0, 1, 2]);

  resolvers[1]();
  resolvers[2]();
  assert.deepEqual(await promise, [0, 1, 2]);
});

test('starts each item exactly once', async () => {
  const counts = new Map();
  await mapLimit([1, 2, 3], 5, item => {
    counts.set(item, (counts.get(item) || 0) + 1);
    return item;
  });
  assert.deepEqual([...counts.values()], [1, 1, 1]);
});

test('does not mutate items', async () => {
  const items = [1, 2, 3];
  const copy = items.slice();
  await mapLimit(items, 2, x => x);
  assert.deepEqual(items, copy);
});

test('rejects TypeError for invalid arguments before invoking worker (empty input too)', async () => {
  let called = 0;
  const worker = () => {
    called += 1;
    return 1;
  };
  await assert.rejects(() => mapLimit('nope', 1, worker), TypeError);
  await assert.rejects(() => mapLimit([1], 0, worker), TypeError);
  await assert.rejects(() => mapLimit([1], 1.5, worker), TypeError);
  await assert.rejects(() => mapLimit([1], -1, worker), TypeError);
  await assert.rejects(() => mapLimit([1], 1, 'nope'), TypeError);
  await assert.rejects(() => mapLimit([], 0, worker), TypeError);
  assert.equal(called, 0);
});

test('propagates synchronous throw with identity and drains started work', async () => {
  const boom = new Error('sync boom');
  const settled = [];
  const error = await mapLimit([0, 1, 2, 3, 4], 2, item => {
    if (item === 1) throw boom;
    return new Promise(resolve =>
      setTimeout(() => {
        settled.push(item);
        resolve(item);
      }, 5)
    );
  }).then(
    () => assert.fail('expected rejection'),
    e => e
  );
  assert.equal(error, boom);
  // Item 2 must never start after the failure is observed.
  assert.deepEqual(settled, [0]);
});

test('rejects with the first observed async error and preserves identity', async () => {
  const first = new Error('first');
  const second = new Error('second');
  const error = await mapLimit([0, 1], 1, item => {
    if (item === 0) return Promise.reject(first);
    return Promise.reject(second);
  }).then(
    () => assert.fail('expected rejection'),
    e => e
  );
  assert.equal(error, first);
});

test('secondary failures do not produce unhandled rejections while draining', async () => {
  const unhandled = [];
  const onUnhandled = reason => unhandled.push(reason);
  process.on('unhandledRejection', onUnhandled);
  try {
    const primary = new Error('primary');
    const secondary = new Error('secondary');
    await mapLimit([0, 1, 2], 3, item => {
      if (item === 0) return Promise.reject(primary);
      return new Promise((_, reject) =>
        setTimeout(() => reject(secondary), 5)
      );
    }).catch(() => {});
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.deepEqual(unhandled, []);
  } finally {
    process.removeListener('unhandledRejection', onUnhandled);
  }
});

test('already-aborted signal rejects with signal.reason and starts no work', async () => {
  const reason = new Error('pre-aborted');
  const controller = new AbortController();
  controller.abort(reason);
  let called = 0;
  const error = await mapLimit([1, 2, 3], 2, x => {
    called += 1;
    return x;
  }, {signal: controller.signal}).then(
    () => assert.fail('expected rejection'),
    e => e
  );
  assert.equal(error, reason);
  assert.equal(called, 0);
});

test('already-aborted signal rejects on empty input too', async () => {
  const reason = 'non-error reason';
  const controller = new AbortController();
  controller.abort(reason);
  const error = await mapLimit([], 2, x => x, {signal: controller.signal}).then(
    () => assert.fail('expected rejection'),
    e => e
  );
  assert.equal(error, reason);
});

test('mid-run abort stops scheduling and waits for in-flight work', async () => {
  const reason = new Error('aborted mid-run');
  const controller = new AbortController();
  const started = [];
  const finished = [];
  const promise = mapLimit([0, 1, 2, 3, 4, 5], 2, item => {
    started.push(item);
    return new Promise(resolve =>
      setTimeout(() => {
        finished.push(item);
        resolve(item);
      }, 10)
    );
  }, {signal: controller.signal});

  setTimeout(() => controller.abort(reason), 1);

  const error = await promise.then(
    () => assert.fail('expected rejection'),
    e => e
  );
  assert.equal(error, reason);
  // Only the two in-flight items started, and both drained before rejecting.
  assert.deepEqual(started, [0, 1]);
  assert.deepEqual(finished, [0, 1]);
});

test('mid-run abort with non-Error reason preserves identity', async () => {
  const controller = new AbortController();
  const drained = [];
  const promise = mapLimit([0, 1, 2], 2, item =>
    new Promise(resolve =>
      setTimeout(() => {
        drained.push(item);
        resolve(item);
      }, 10)
    ),
  {signal: controller.signal});

  controller.abort('custom reason');

  const error = await promise.then(
    () => assert.fail('expected rejection'),
    e => e
  );
  assert.equal(error, 'custom reason');
  assert.deepEqual(drained, [0, 1]);
});

test('abort wins even when an in-flight worker later rejects', async () => {
  const controller = new AbortController();
  const workerError = new Error('worker lost the race');
  const promise = mapLimit([0, 1], 2, () =>
    new Promise((_, reject) => setTimeout(() => reject(workerError), 10)),
  {signal: controller.signal});

  controller.abort('abort wins');
  const error = await promise.then(
    () => assert.fail('expected rejection'),
    e => e
  );
  assert.equal(error, 'abort wins');
});

test('removes the installed abort listener after settlement', async () => {
  const controller = new AbortController();
  const signal = controller.signal;
  let added = 0;
  let removed = 0;
  const originalAdd = signal.addEventListener.bind(signal);
  const originalRemove = signal.removeEventListener.bind(signal);
  signal.addEventListener = (type, listener, opts) => {
    if (type === 'abort') added += 1;
    return originalAdd(type, listener, opts);
  };
  signal.removeEventListener = (type, listener, opts) => {
    if (type === 'abort') removed += 1;
    return originalRemove(type, listener, opts);
  };

  await mapLimit([1, 2], 2, x => x, {signal});
  assert.equal(added, 1);
  assert.equal(removed, 1);

  await mapLimit([1, 2], 2, x => Promise.reject(new Error('nope')), {signal}).catch(() => {});
  assert.equal(added, 2);
  assert.equal(removed, 2);
});
