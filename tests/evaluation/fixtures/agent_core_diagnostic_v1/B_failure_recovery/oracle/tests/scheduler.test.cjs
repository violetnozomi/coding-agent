const test = require('node:test');
const assert = require('node:assert/strict');
const { schedule, summarize, RetryableError } = require('../index.cjs');
test('legacy failure identity and metrics', {timeout:1000}, async () => {
  const error = new Error('bad');
  const results = await schedule([{id:'a',payload:1}], () => {throw error;});
  assert.equal(results[0].error,error);
  assert.deepEqual(summarize(results), {succeeded:0,failed:1,attempts:1});
});
test('exhaustion retains final error', {timeout:1000}, async () => {
  let final; let count=0;
  const result=await schedule([{id:'a',payload:1,maxAttempts:3}],()=>{
    final=new RetryableError(String(++count)); throw final;
  });
  assert.equal(result[0].error,final); assert.equal(result[0].attempts,3);
});
