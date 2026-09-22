'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {processBatch} = require('../index.cjs');

const tick = () => new Promise((resolve) => setImmediate(resolve));

test('batch maps ids', async () => {
  assert.deepEqual(await processBatch([{id: 'a'}, {id: 'b'}], async (x) => x.id), [
    {id: 'a', value: 'a'},
    {id: 'b', value: 'b'},
  ]);
});

test('processBatch preserves input order for out-of-order completion', async () => {
  const records = [{id: 'a'}, {id: 'b'}, {id: 'c'}];
  const delays = {a: 30, b: 1, c: 10};
  const result = await processBatch(records, async (record) => {
    await new Promise((resolve) => setTimeout(resolve, delays[record.id]));
    return record.id.toUpperCase();
  });
  assert.deepEqual(result, [
    {id: 'a', value: 'A'},
    {id: 'b', value: 'B'},
    {id: 'c', value: 'C'},
  ]);
});

test('default concurrency is 2', async () => {
  let inFlight = 0;
  let peak = 0;
  await processBatch([{id: 1}, {id: 2}, {id: 3}, {id: 4}], async (record) => {
    inFlight += 1;
    peak = Math.max(peak, inFlight);
    await tick();
    inFlight -= 1;
    return record.id;
  });
  assert.equal(peak, 2);
});

test('options.concurrency bounds in-flight sends', async () => {
  let inFlight = 0;
  let peak = 0;
  await processBatch([{id: 1}, {id: 2}, {id: 3}, {id: 4}, {id: 5}], async (record) => {
    inFlight += 1;
    peak = Math.max(peak, inFlight);
    await tick();
    inFlight -= 1;
    return record.id;
  }, {concurrency: 4});
  assert.equal(peak, 4);
});

test('sends each record at most once', async () => {
  const records = [{id: 'a'}, {id: 'b'}, {id: 'c'}];
  const calls = records.map(() => 0);
  await processBatch(records, (record, index) => {
    calls[index] += 1;
    return record.id;
  });
  assert.deepEqual(calls, [1, 1, 1]);
});

test('propagates send rejection', async () => {
  const boom = new Error('send failed');
  await assert.rejects(
    () => processBatch([{id: 'a'}, {id: 'b'}], async () => { throw boom; }),
    (error) => error === boom,
  );
});

test('propagates cancellation via options.signal', async () => {
  const controller = new AbortController();
  const reason = new Error('cancelled');
  controller.abort(reason);
  let calls = 0;
  await assert.rejects(
    () => processBatch([{id: 'a'}], () => { calls += 1; return 'x'; }, {signal: controller.signal}),
    (error) => error === reason,
  );
  assert.equal(calls, 0);
});

test('send receives the signal and each record at most once on mid-run abort', async () => {
  const controller = new AbortController();
  const reason = new Error('mid');
  const sent = [];
  const seenSignals = [];

  const promise = processBatch(
    [{id: 1}, {id: 2}, {id: 3}, {id: 4}],
    async (record, index, signal) => {
      seenSignals.push(signal);
      sent.push(record.id);
      await new Promise((resolve) => setTimeout(resolve, 10));
      return record.id;
    },
    {concurrency: 2, signal: controller.signal},
  );

  setTimeout(() => controller.abort(reason), 5);
  await assert.rejects(() => promise, (error) => error === reason);
  assert.deepEqual(sent, [1, 2]);
  assert.ok(seenSignals.every((signal) => signal === controller.signal));
});
