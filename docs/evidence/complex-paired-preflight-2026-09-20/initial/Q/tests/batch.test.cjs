const test = require('node:test');
const assert = require('node:assert/strict');
const {processBatch} = require('../index.cjs');
test('batch maps ids', async () => {
  assert.deepEqual(await processBatch([{id:'a'}, {id:'b'}], async x => x.id),
    [{id:'a',value:'a'}, {id:'b',value:'b'}]);
});
