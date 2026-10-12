const test = require('node:test');
const assert = require('node:assert/strict');
const value = require('./index.js');
test('value and exported fields', () => {
 assert.equal(value(), 5); assert.equal(value.left, 2); assert.equal(value.right, 3);
});
