'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { processBatch } = require('../index.cjs');

const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

test('batch maps ids', async () => {
  assert.deepEqual(await processBatch([{ id: 'a' }, { id: 'b' }], async (x) => x.id),
    [{ id: 'a', value: 'a' }, { id: 'b', value: 'b' }]);
});

test('defaults to concurrency 2 and preserves input order', async () => {
  let active = 0;
  let maxActive = 0;
  const records = [0, 1, 2, 3, 4].map((id) => ({ id }));
  const out = await processBatch(records, async (record) => {
    active++;
    maxActive = Math.max(maxActive, active);
    await tick(5);
    active--;
    return record.id;
  });
  assert.equal(maxActive, 2);
  assert.deepEqual(out, records.map((record) => ({ id: record.id, value: record.id })));
});

test('honours options.concurrency', async () => {
  let active = 0;
  let maxActive = 0;
  const records = [1, 2, 3, 4, 5, 6].map((id) => ({ id }));
  const out = await processBatch(records, async (record) => {
    active++;
    maxActive = Math.max(maxActive, active);
    await tick(5);
    active--;
    return record.id * 2;
  }, { concurrency: 3 });
  assert.equal(maxActive, 3);
  assert.deepEqual(out.map((entry) => entry.value), [2, 4, 6, 8, 10, 12]);
});

test('sends each record at most once', async () => {
  const seen = [];
  await processBatch([{ id: 1 }, { id: 2 }, { id: 3 }], async (record) => {
    seen.push(record.id);
    return record.id;
  }, { concurrency: 2 });
  assert.deepEqual(seen.slice().sort(), [1, 2, 3]);
  assert.equal(seen.length, 3);
});

test('preserves falsy send results', async () => {
  const out = await processBatch([{ id: 'a' }, { id: 'b' }], async (record) => (
    record.id === 'a' ? 0 : ''
  ), { concurrency: 2 });
  assert.deepEqual(out, [{ id: 'a', value: 0 }, { id: 'b', value: '' }]);
});

test('propagates a send rejection with identity and stops scheduling', async () => {
  const boom = new Error('send failed');
  const sent = [];
  await assert.rejects(
    processBatch([{ id: 1 }, { id: 2 }, { id: 3 }, { id: 4 }], async (record) => {
      sent.push(record.id);
      if (record.id === 1) throw boom;
      return record.id;
    }, { concurrency: 2 }),
    (err) => err === boom,
  );
  assert.deepEqual(sent, [1, 2]); // 3 and 4 never started
});

test('cancels in-flight work via options.signal', async () => {
  const controller = new AbortController();
  const reason = new Error('cancel batch');
  const sent = [];
  const promise = processBatch([{ id: 1 }, { id: 2 }, { id: 3 }], async (record) => {
    sent.push(record.id);
    await tick(10);
    return record.id;
  }, { concurrency: 2, signal: controller.signal });

  controller.abort(reason);
  await assert.rejects(promise, (err) => err === reason);
  assert.deepEqual(sent, [1, 2]);
});

test('forwards the signal to send for cooperative cancellation', async () => {
  const controller = new AbortController();
  let received;
  await processBatch([{ id: 1 }], async (record, signal) => {
    received = signal;
    return record.id;
  }, { signal: controller.signal });
  assert.equal(received, controller.signal);
});

test('rejects invalid concurrency through mapLimit validation', async () => {
  await assert.rejects(
    processBatch([{ id: 1 }], async (record) => record.id, { concurrency: 0 }),
    TypeError,
  );
});

test('rejects a non-callable send with TypeError', async () => {
  await assert.rejects(processBatch([{ id: 1 }], null), TypeError);
});

test('empty records resolve to []', async () => {
  assert.deepEqual(await processBatch([], async (record) => record.id), []);
});
