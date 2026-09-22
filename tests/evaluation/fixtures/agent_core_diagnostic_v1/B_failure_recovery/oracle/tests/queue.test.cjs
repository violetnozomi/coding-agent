const test = require('node:test');
const assert = require('node:assert/strict');
const { Queue, RetryableError } = require('../index.cjs');
test('FIFO legacy jobs', {timeout: 1000}, async () => {
  const q = new Queue(); q.enqueue({id:'a',payload:1}); q.enqueue({id:'b',payload:2});
  const seen = [];
  const result = await q.run(x => { seen.push(x); return x * 2; });
  assert.deepEqual(seen, [1,2]);
  assert.deepEqual(result, [{id:'a',ok:true,value:2,attempts:1},{id:'b',ok:true,value:4,attempts:1}]);
});
test('FIFO and retry fairness', {timeout:1000}, async () => {
  const q = new Queue(); q.enqueue({id:'a',payload:'a',maxAttempts:2}); q.enqueue({id:'b',payload:'b'});
  const seen = [];
  const result = await q.run(x => {
    seen.push(x);
    if (x === 'a' && seen.length === 1) throw new RetryableError('again');
    return x;
  });
  assert.deepEqual(seen, ['a','b','a']);
  assert.deepEqual(result.map(x=>x.attempts), [2,1]);
  assert.deepEqual(result.map(x=>x.id), ['a','b']);
});
test('invalid policy never enqueues', () => {
  const q = new Queue();
  assert.throws(()=>q.enqueue({id:'bad',payload:1,maxAttempts:0}), RangeError);
});
