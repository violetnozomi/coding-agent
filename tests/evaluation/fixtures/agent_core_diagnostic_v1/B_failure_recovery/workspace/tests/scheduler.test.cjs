const test = require('node:test');
const assert = require('node:assert/strict');
const { schedule, summarize } = require('../index.cjs');
test('failure identity and metrics', {timeout:1000}, async () => {
  const error = new Error('bad');
  const results = await schedule([{id:'a',payload:1}], () => {throw error;});
  assert.equal(results[0].error, error);
  assert.deepEqual(summarize(results), {succeeded:0,failed:1,attempts:1});
});
