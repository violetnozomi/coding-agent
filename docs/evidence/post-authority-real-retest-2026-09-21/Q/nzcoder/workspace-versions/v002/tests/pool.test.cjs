const test = require('node:test');
const assert = require('node:assert/strict');
const {mapLimit} = require('../index.cjs');

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return {promise, resolve, reject};
}

function trackAbortListeners(signal) {
  const counts = {added: 0, removed: 0};
  const add = signal.addEventListener.bind(signal);
  const remove = signal.removeEventListener.bind(signal);
  signal.addEventListener = (type, fn, opts) => {
    if (type === 'abort') counts.added += 1;
    return add(type, fn, opts);
  };
  signal.removeEventListener = (type, fn, opts) => {
    if (type === 'abort') counts.removed += 1;
    return remove(type, fn, opts);
  };
  return counts;
}

test('maps values and preserves falsy results', async () => {
  assert.deepEqual(await mapLimit([1, 2], 2, x => x - 1), [0, 1]);
  assert.deepEqual(await mapLimit([1, 2, 3], 2, () => 0), [0, 0, 0]);
  assert.deepEqual(await mapLimit([1, 2, 3], 1, () => false), [false, false, false]);
  assert.deepEqual(await mapLimit([1, 2], 2, () => null), [null, null]);
  assert.deepEqual(await mapLimit([1, 2], 2, () => ''), ['', '']);
});

test('empty input', async () => assert.deepEqual(await mapLimit([], 2, x => x), []));

test('rejects invalid arguments with TypeError before invoking the worker', async () => {
  let calls = 0;
  const worker = () => {
    calls += 1;
  };

  await assert.rejects(() => mapLimit('nope', 2, worker), TypeError);
  await assert.rejects(() => mapLimit(null, 2, worker), TypeError);
  await assert.rejects(() => mapLimit([1], 0, worker), TypeError);
  await assert.rejects(() => mapLimit([1], -1, worker), TypeError);
  await assert.rejects(() => mapLimit([1], 1.5, worker), TypeError);
  await assert.rejects(() => mapLimit([1], '2', worker), TypeError);
  await assert.rejects(() => mapLimit([1], 2, 'nope'), TypeError);

  // Validation must also happen for empty input, before any worker runs.
  await assert.rejects(() => mapLimit([], 0, worker), TypeError);
  await assert.rejects(() => mapLimit([], -3, worker), TypeError);
  await assert.rejects(() => mapLimit('', 2, worker), TypeError);

  assert.equal(calls, 0);
});

test('never mutates the input array and reports (item, index, signal)', async () => {
  const items = [3, 1, 2];
  const snapshot = items.slice();
  const seen = [];
  const signal = new AbortController().signal;

  const results = await mapLimit(items, 2, (item, index, received) => {
    seen.push([item, index, received]);
    return item * 10;
  }, {signal});

  assert.deepEqual(results, [30, 10, 20]);
  assert.deepEqual(items, snapshot);
  assert.deepEqual(seen.map((entry) => entry[1]).sort((a, b) => a - b), [0, 1, 2]);
  assert.deepEqual(seen.map((entry) => entry[0]).sort((a, b) => a - b), [1, 2, 3]);
  for (const entry of seen) assert.equal(entry[2], signal);
});

test('caps in-flight workers at limit and fills free slots eagerly', async () => {
  const gate = deferred();
  const started = [];
  let inFlight = 0;
  let peak = 0;

  const running = mapLimit([0, 1, 2, 3, 4, 5], 2, async (item) => {
    started.push(item);
    inFlight += 1;
    peak = Math.max(peak, inFlight);
    await gate.promise;
    inFlight -= 1;
    return item;
  });

  // Exactly `limit` workers start without waiting for the whole batch.
  await Promise.resolve();
  assert.deepEqual(started, [0, 1]);

  gate.resolve();
  assert.deepEqual(await running, [0, 1, 2, 3, 4, 5]);
  assert.equal(peak, 2);
  assert.deepEqual(started, [0, 1, 2, 3, 4, 5]);
});

test('starts every item exactly once even when limit exceeds the workload', async () => {
  const started = [];
  const results = await mapLimit([9, 8, 7], 10, (item, index) => {
    started.push(index);
    return item;
  });
  assert.deepEqual(results, [9, 8, 7]);
  assert.deepEqual(started, [0, 1, 2]);
});

test('drains already-started work, stops scheduling and rethrows the first error', async () => {
  const first = new Error('first boom');
  const second = new Error('second boom');
  const gate = deferred();
  const started = [];
  let release = 0;

  const running = mapLimit([0, 1, 2, 3, 4], 2, async (item) => {
    started.push(item);
    if (item === 0) throw first;
    if (item === 1) {
      await gate.promise;
      throw second;
    }
    release += 1;
    return item;
  });

  const observed = await running.then(
    () => {
      throw new Error('expected mapLimit to reject');
    },
    (error) => error
  );

  assert.equal(observed, first);
  // Item 1 was already in flight, so it must be allowed to settle...
  assert.deepEqual(started, [0, 1]);
  // ...and no further item is ever started.
  assert.equal(release, 0);
  gate.resolve();
});

test('rejects with the first error even when a later failure is observed first', async () => {
  const slow = new Error('slow');
  const fast = new Error('fast');
  const gate = deferred();

  await assert.rejects(
    mapLimit([0, 1], 2, async (item) => {
      if (item === 0) {
        await gate.promise;
        throw slow;
      }
      // Observed before the other slot settles, but not the first failure
      // in scheduling order? Here it *is* observed first.
      throw fast;
    }),
    (error) => error === fast
  );
  gate.resolve();
});

test('waits for in-flight work to settle before rejecting', async () => {
  let drained = false;
  const gate = deferred();

  const running = mapLimit([0, 1], 2, async (item) => {
    if (item === 0) throw new Error('boom');
    await gate.promise;
    drained = true;
    return item;
  });

  let settled = false;
  running.catch(() => {
    settled = true;
  });

  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(settled, false, 'must not reject while a worker is still in flight');
  assert.equal(drained, false);

  gate.resolve();
  await running.catch(() => {});
  assert.equal(drained, true);
});

test('already-aborted signal starts no work and preserves the reason identity', async () => {
  const controller = new AbortController();
  const reason = {code: 'pre-aborted'};
  controller.abort(reason);
  let started = 0;

  await assert.rejects(
    mapLimit([1, 2, 3], 2, () => {
      started += 1;
      return 1;
    }, {signal: controller.signal}),
    (error) => error === reason
  );
  assert.equal(started, 0);

  // Also with an empty workload.
  await assert.rejects(
    mapLimit([], 2, () => 1, {signal: controller.signal}),
    (error) => error === reason
  );
});

test('mid-run abort stops scheduling, drains in-flight work and preserves reason', async () => {
  const controller = new AbortController();
  const reason = new Error('cancelled');
  const gate = deferred();
  const started = [];

  const running = mapLimit([0, 1, 2, 3], 2, async (item) => {
    started.push(item);
    await gate.promise;
    return item;
  }, {signal: controller.signal});

  await Promise.resolve();
  assert.deepEqual(started, [0, 1]);

  controller.abort(reason);

  let settled = false;
  running.catch(() => {
    settled = true;
  });
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(settled, false, 'in-flight workers must be allowed to drain');

  gate.resolve();
  await assert.rejects(running, (error) => error === reason);
  assert.deepEqual(started, [0, 1]);
});

test('a synchronous worker throw is a first-class failure', async () => {
  const boom = new Error('sync boom');
  const started = [];

  await assert.rejects(
    mapLimit([0, 1, 2], 1, (item) => {
      started.push(item);
      throw boom;
    }),
    (error) => error === boom
  );
  assert.deepEqual(started, [0]);
});

test('the first observed failure wins over a later abort', async () => {
  const controller = new AbortController();
  const boom = new Error('worker boom');
  const abortReason = new Error('abort reason');
  const gate = deferred();

  const running = mapLimit([0, 1, 2], 2, async (item) => {
    if (item === 0) throw boom;
    await gate.promise;
    return item;
  }, {signal: controller.signal});

  await new Promise((resolve) => setTimeout(resolve, 5));
  controller.abort(abortReason);
  gate.resolve();

  await assert.rejects(running, (error) => error === boom);
});

test('secondary failures do not become unhandled rejections', async () => {
  const unhandled = [];
  const onUnhandled = (reason) => unhandled.push(reason);
  process.on('unhandledRejection', onUnhandled);

  try {
    const first = new Error('first');
    const second = new Error('second');
    const gate = deferred();

    await assert.rejects(
      mapLimit([0, 1], 2, async (item) => {
        if (item === 0) {
          await gate.promise;
          throw first;
        }
        throw second;
      }),
      (error) => error === second
    );
    gate.resolve();

    // Give any stray rejection a chance to surface.
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.deepEqual(unhandled, []);
  } finally {
    process.removeListener('unhandledRejection', onUnhandled);
  }
});

test('removes the abort listener after settling', async () => {
  const controller = new AbortController();
  const counts = trackAbortListeners(controller.signal);

  await mapLimit([1, 2], 2, (x) => x, {signal: controller.signal});
  assert.equal(counts.added, 1);
  assert.equal(counts.removed, 1);

  const failing = new AbortController();
  const failingCounts = trackAbortListeners(failing.signal);
  await assert.rejects(
    mapLimit([1], 1, () => {
      throw new Error('nope');
    }, {signal: failing.signal})
  );
  assert.equal(failingCounts.added, 1);
  assert.equal(failingCounts.removed, 1);
});

test('options and signal are optional', async () => {
  assert.deepEqual(await mapLimit([1, 2], 2, (x) => x * 2), [2, 4]);
  assert.deepEqual(await mapLimit([1, 2], 2, (x) => x * 2, {}), [2, 4]);
  assert.deepEqual(await mapLimit([1, 2], 2, (x, _i, signal) => signal, {signal: undefined}), [
    undefined,
    undefined
  ]);
});
