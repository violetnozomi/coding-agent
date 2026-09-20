const test = require('node:test');
const assert = require('node:assert/strict');
const escape = require('./index.js');

test('ordinary literals still match', () => {
  for (const value of ['a.b', '[x]', 'a+b', 'a\\b']) {
    assert.equal(new RegExp('^' + escape(value) + '$').test(value), true);
  }
});
test('literals can be embedded in Unicode regular expressions', () => {
  for (const value of ['a-b', '[]-x', 'foo.bar']) {
    assert.equal(new RegExp('^' + escape(value) + '$', 'u').test(value), true);
  }
});
test('non-string inputs remain rejected', () => {
  for (const value of [null, 3, {}]) assert.throws(() => escape(value), TypeError);
});
