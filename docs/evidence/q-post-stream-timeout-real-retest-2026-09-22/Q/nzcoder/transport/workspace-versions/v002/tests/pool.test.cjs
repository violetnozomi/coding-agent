'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {mapLimit} = require('../index.cjs');

const tick = () => new Promise((resolve) => setImmediate(resolve));

test('maps values and preserves falsy results', async () => {
  assert.deepEqual(await mapLimit([1, 2], 2, (x) => x - 1), [0, 1]);
});

test('empty input', async () => {
  assert.deepEqual(await mapLimit([], 2, (x) => x), []);
});

test('empty input still validates arguments', async () => {
  await assert.rejects(() => mapLimit([], 0, () => 1), TypeError);
  await assert.rejects(() => mapLimit([], -1, () => 1), TypeError);
  await assert.rejects(() => mapLimit('nope', 2, () => 1), TypeError);
  await assert.rejects(() => mapLimit([], 2, 'nope'), TypeError);
  await assert.rejects(() => mapLimit([], 2.5, () => 1), TypeError);
});

test('validates before invoking any worker', async () => {
  let called = 0;
  await assert.rejects(
    () =>
      mapLimit([1, 2], 0, () => {
        called += 1;
        return 1;
      }),
    TypeError,
  );
  assert.equal(called, 0);
});

test('results are in input order even when completion order differs', async () => {
  const delays = [30, 1, 20, 2];
  const results = await mapLimit([0, 1, 2, 3], 2, async (index) => {
    await new Promise((resolve) => setTimeout(resolve, delays[index]));
    return `v${index}`;
  });
  assert.deepEqual(results, ['v0', 'v1', 'v2', 'v3']);
});

test('does not mutate the input array', async () => {
  const items = [3, 1, 2];
  const copy = items.slice();
  await mapLimit(items, 2, (x) => x * 2);
  assert.deepEqual(items, copy);
});

test('never exceeds the concurrency limit and fills available slots', async () => {
  let inFlight = 0;
  let peak = 0;
  const started = [];
  const results = await mapLimit([1, 2, 3, 4, 5, 6], 3, async (item) => {
    inFlight += 1;
    peak = Math.max(peak, inFlight);
    started.push(item);
    if (started.length === 3) {
      // All three initial slots must be started before any of them finish.
      assert.deepEqual(started.slice(0, 3), [1, 2, 3]);
    }
    await tick();
    inFlight -= 1;
    return item * 10;
  });
  assert.equal(peak, 3);
  assert.deepEqual(results, [10, 20, 30, 40, 50, 60]);
});

test('starts each item exactly once', async () => {
  const seen = [];
  await mapLimit([1, 2, 3, 4], 2, (item) => {
    seen.push(item);
    return item;
  });
  assert.deepEqual(seen.slice().sort((a, b) => a - b), [1, 2, 3, 4]);
});

test('worker receives item, index and signal', async () => {
  const controller = new AbortController();
  const observed = [];
  await mapLimit(['a', 'b'], 1, (item, index, signal) => {
    observed.push([item, index, signal]);
    return item;
  }, {signal: controller.signal});
  assert.deepEqual(observed.map(([item, index]) => [item, index]), [['a', 0], ['b', 1]]);
  assert.equal(observed[0][2], controller.signal);
});

test('supports synchronous workers and synchronous throws', async () => {
  assert.deepEqual(await mapLimit([1, 2], 2, (x) => x + 1), [2, 3]);
  const boom = new Error('sync');
  await assert.rejects(() => mapLimit([1], 1, () => { throw boom; }), (error) => error === boom);
});

test('drains in-flight work and rejects with the first observed error', async () => {
  const first = new Error('first');
  const second = new Error('second');
  const settled = [];
  let started = 0;

  await assert.rejects(
    () =>
      mapLimit([0, 1, 2, 3], 2, async (index) => {
        started += 1;
        if (index === 0) {
          await tick();
          throw first;
        }
        if (index === 1) {
          await new Promise((resolve) => setTimeout(resolve, 20));
          throw second;
        }
        settled.push(index);
        return index;
      }),
    (error) => error === first,
  );

  // Item 0 failed first, so items 2 and 3 must never be started.
  assert.equal(started, 2);
  assert.deepEqual(settled, []);
});

test('secondary failures do not cause unhandled rejections', async () => {
  const unhandled = [];
  const onUnhandled = (reason) => unhandled.push(reason);
  process.on('unhandledRejection', onUnhandled);
  try {
    const first = new Error('first');
    await assert.rejects(
      () => mapLimit([0, 1], 2, async (index) => {
        await tick();
        throw index === 0 ? first : new Error('secondary');
      }),
      (error) => error === first,
    );
    await new Promise((resolve) => setTimeout(resolve, 20));
  } finally {
    process.removeListener('unhandledRejection', onUnhandled);
  }
  assert.deepEqual(unhandled, []);
});

test('already-aborted signal starts no work and rejects with the reason', async () => {
  const controller = new AbortController();
  const reason = new Error('cancelled');
  controller.abort(reason);
  let called = 0;
  await assert.rejects(
    () => mapLimit([1, 2], 2, (x) => { called += 1; return x; }, {signal: controller.signal}),
    (error) => error === reason,
  );
  assert.equal(called, 0);
});

test('already-aborted signal preserves a non-Error reason', async () => {
  const controller = new AbortController();
  controller.abort('stop');
  await assert.rejects(
    () => mapLimit([1], 1, (x) => x, {signal: controller.signal}),
    (error) => error === 'stop',
  );
});

test('already-aborted signal wins over empty input', async () => {
  const controller = new AbortController();
  controller.abort(42);
  await assert.rejects(
    () => mapLimit([], 2, (x) => x, {signal: controller.signal}),
    (error) => error === 42,
  );
});

test('mid-run abort stops scheduling, drains in-flight work and rejects', async () => {
  const controller = new AbortController();
  const reason = new Error('mid-run');
  const completed = [];
  let started = 0;

  const promise = mapLimit([0, 1, 2, 3, 4], 2, async (index) => {
    started += 1;
    await new Promise((resolve) => setTimeout(resolve, 10));
    completed.push(index);
    return index;
  }, {signal: controller.signal});

  setTimeout(() => controller.abort(reason), 5);

  await assert.rejects(() => promise, (error) => error === reason);
  assert.equal(started, 2);
  assert.deepEqual(completed.slice().sort((a, b) => a - b), [0, 1]);
});

test('no abort listener is left installed after settlement', async () => {
  const controller = new AbortController();
  const {signal} = controller;
  const original = signal.removeEventListener.bind(signal);
  let removed = 0;
  signal.removeEventListener = (...args) => {
    removed += 1;
    return original(...args);
  };
  try {
    await mapLimit([1, 2, 3], 3, (x) => x, {signal});
    assert.equal(removed, 1);
  } finally {
    delete signal.removeEventListener;
  }
});
