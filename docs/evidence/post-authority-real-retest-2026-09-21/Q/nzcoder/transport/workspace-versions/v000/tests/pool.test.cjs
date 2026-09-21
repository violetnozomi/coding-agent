const test = require('node:test');
const assert = require('node:assert/strict');
const {mapLimit} = require('../index.cjs');
test('maps values and preserves falsy results', async () => {
  assert.deepEqual(await mapLimit([1, 2], 2, x => x - 1), [0, 1]);
});
test('empty input', async () => assert.deepEqual(await mapLimit([], 2, x => x), []));
