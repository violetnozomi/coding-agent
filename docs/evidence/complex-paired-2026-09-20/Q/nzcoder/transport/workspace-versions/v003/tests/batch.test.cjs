'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {processBatch, mapLimit} = require('../index.cjs');

const tick = () => new Promise(resolve => setImmediate(resolve));

test('batch maps ids', async () => {
  assert.deepEqual(await processBatch([{id: 'a'}, {id: 'b'}], async x => x.id), [
    {id: 'a', value: 'a'},
    {id: 'b', value: 'b'}
  ]);
});

test('index.cjs keeps exporting both mapLimit and processBatch', () => {
  assert.equal(typeof mapLimit, 'function');
  assert.equal(typeof processBatch, 'function');
});

test('returns ordered {id, value} results even when completions race', async () => {
  const records = [{id: 'a'}, {id: 'b'}, {id: 'c'}];
  const delays = {a: 25, b: 0, c: 10};
  const results = await processBatch(records, async record => {
    await new Promise(resolve => setTimeout(resolve, delays[record.id]));
    return record.id.toUpperCase();
  });
  assert.deepEqual(results, [
    {id: 'a', value: 'A'},
    {id: 'b', value: 'B'},
    {id: 'c', value: 'C'}
  ]);
});

test('defaults to concurrency 2', async () => {
  let inFlight = 0;
  let peak = 0;
  await processBatch([{id: 1}, {id: 2}, {id: 3}, {id: 4}], async record => {
    inFlight += 1;
    peak = Math.max(peak, inFlight);
    await tick();
    inFlight -= 1;
    return record.id;
  });
  assert.equal(peak, 2);
});

test('honours options.concurrency', async () => {
  let inFlight = 0;
  let peak = 0;
  await processBatch(
    [{id: 1}, {id: 2}, {id: 3}, {id: 4}],
    async record => {
      inFlight += 1;
      peak = Math.max(peak, inFlight);
      await tick();
      inFlight -= 1;
      return record.id;
    },
    {concurrency: 3}
  );
  assert.equal(peak, 3);
});

test('sends each record at most once', async () => {
  const counts = new Map();
  await processBatch([{id: 'a'}, {id: 'b'}, {id: 'c'}], record => {
    counts.set(record.id, (counts.get(record.id) || 0) + 1);
    return record.id;
  });
  assert.deepEqual([...counts.entries()], [
    ['a', 1],
    ['b', 1],
    ['c', 1]
  ]);
});

test('propagates send rejection with identity', async () => {
  const boom = new Error('send failed');
  const error = await processBatch([{id: 'a'}, {id: 'b'}], record => {
    if (record.id === 'b') return Promise.reject(boom);
    return record.id;
  }).then(
    () => assert.fail('expected rejection'),
    e => e
  );
  assert.equal(error, boom);
});

test('forwards options.signal to mapLimit and to send', async () => {
  const reason = new Error('batch aborted');
  const controller = new AbortController();
  const signals = [];
  const promise = processBatch(
    [{id: 'a'}, {id: 'b'}, {id: 'c'}],
    (record, index, signal) => {
      signals.push(signal);
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => resolve(record.id), 20);
        signal.addEventListener('abort', () => {
          clearTimeout(timer);
          reject(signal.reason);
        });
      });
    },
    {concurrency: 1, signal: controller.signal}
  );
  controller.abort(reason);
  const error = await promise.then(
    () => assert.fail('expected rejection'),
    e => e
  );
  assert.equal(error, reason);
  assert.equal(signals.length, 1);
  assert.equal(signals[0], controller.signal);
});

test('already-aborted signal starts no sends and rejects with the reason', async () => {
  const reason = new Error('pre-aborted');
  const controller = new AbortController();
  controller.abort(reason);
  let calls = 0;
  const error = await processBatch(
    [{id: 'a'}, {id: 'b'}],
    record => {
      calls += 1;
      return record.id;
    },
    {signal: controller.signal}
  ).then(
    () => assert.fail('expected rejection'),
    e => e
  );
  assert.equal(error, reason);
  assert.equal(calls, 0);
});
