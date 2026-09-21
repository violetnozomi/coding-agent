const test = require('node:test');
const assert = require('node:assert/strict');
const {processBatch} = require('../index.cjs');

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return {promise, resolve, reject};
}

test('batch maps ids', async () => {
  assert.deepEqual(await processBatch([{id: 'a'}, {id: 'b'}], async x => x.id),
    [{id: 'a', value: 'a'}, {id: 'b', value: 'b'}]);
});

test('returns ordered records regardless of completion order', async () => {
  const gates = [deferred(), deferred(), deferred()];
  const records = [{id: 'a'}, {id: 'b'}, {id: 'c'}];

  const running = processBatch(records, async (record) => {
    const index = records.indexOf(record);
    await gates[index].promise;
    return record.id.toUpperCase();
  });

  // Resolve out of order.
  gates[2].resolve();
  gates[0].resolve();
  gates[1].resolve();

  assert.deepEqual(await running, [
    {id: 'a', value: 'A'},
    {id: 'b', value: 'B'},
    {id: 'c', value: 'C'}
  ]);
});

test('sends each record at most once', async () => {
  const sent = [];
  const records = [{id: 'a'}, {id: 'b'}, {id: 'c'}, {id: 'd'}];

  await processBatch(records, async (record) => {
    sent.push(record.id);
    return 1;
  });

  assert.deepEqual(sent.slice().sort(), ['a', 'b', 'c', 'd']);
  assert.equal(sent.length, records.length);
});

test('defaults to concurrency 2 and honours options.concurrency', async () => {
  const gate = deferred();

  let inFlight = 0;
  let peakDefault = 0;
  const defaultRun = processBatch([{id: 1}, {id: 2}, {id: 3}, {id: 4}], async () => {
    inFlight += 1;
    peakDefault = Math.max(peakDefault, inFlight);
    await gate.promise;
    inFlight -= 1;
    return true;
  });
  await Promise.resolve();
  assert.equal(peakDefault, 2);
  gate.resolve();
  await defaultRun;

  const gate2 = deferred();
  let peakCustom = 0;
  let inFlight2 = 0;
  const customRun = processBatch(
    [{id: 1}, {id: 2}, {id: 3}, {id: 4}],
    async () => {
      inFlight2 += 1;
      peakCustom = Math.max(peakCustom, inFlight2);
      await gate2.promise;
      inFlight2 -= 1;
      return true;
    },
    {concurrency: 1}
  );
  await Promise.resolve();
  assert.equal(peakCustom, 1);
  gate2.resolve();
  await customRun;
});

test('propagates send rejections with identity and drains in-flight work', async () => {
  const boom = new Error('send failed');
  const gate = deferred();
  const started = [];

  const running = processBatch(
    [{id: 'a'}, {id: 'b'}, {id: 'c'}, {id: 'd'}],
    async (record) => {
      started.push(record.id);
      if (record.id === 'a') throw boom;
      await gate.promise;
      return record.id;
    },
    {concurrency: 2}
  );

  await assert.rejects(running, (error) => error === boom);
  assert.deepEqual(started, ['a', 'b']);
  gate.resolve();
});

test('cancels via options.signal and preserves the abort reason', async () => {
  const controller = new AbortController();
  const reason = new Error('batch cancelled');
  const gate = deferred();
  const started = [];

  const running = processBatch(
    [{id: 'a'}, {id: 'b'}, {id: 'c'}],
    async (record) => {
      started.push(record.id);
      await gate.promise;
      return record.id;
    },
    {concurrency: 1, signal: controller.signal}
  );

  await Promise.resolve();
  controller.abort(reason);

  let settled = false;
  running.catch(() => {
    settled = true;
  });
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(settled, false, 'in-flight send must drain before rejecting');

  gate.resolve();
  await assert.rejects(running, (error) => error === reason);
  assert.deepEqual(started, ['a']);
});

test('an already-aborted signal sends nothing', async () => {
  const controller = new AbortController();
  const reason = {code: 'pre-aborted'};
  controller.abort(reason);
  let calls = 0;

  await assert.rejects(
    processBatch([{id: 'a'}], () => {
      calls += 1;
      return 1;
    }, {signal: controller.signal}),
    (error) => error === reason
  );
  assert.equal(calls, 0);
});

test('rejects invalid arguments with TypeError', async () => {
  await assert.rejects(() => processBatch('nope', async () => 1), TypeError);
  await assert.rejects(() => processBatch([{id: 'a'}], 'nope'), TypeError);
  await assert.rejects(() => processBatch([{id: 'a'}], async () => 1, {concurrency: 0}), TypeError);
});

test('empty input resolves to an empty list', async () => {
  assert.deepEqual(await processBatch([], async () => 1), []);
});
