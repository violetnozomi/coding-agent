'use strict';

const {mapLimit} = require('./pool.cjs');

const DEFAULT_CONCURRENCY = 2;

/**
 * processBatch(records, send, options = {}) -> Promise<Array<{id, value}>>
 *
 * Runs `send(record)` for every record with bounded concurrency
 * (options.concurrency, default 2), forwards options.signal for
 * cancellation and resolves ordered {id, value} records. Every record is
 * sent at most once, and a failure/abort propagates as a rejection.
 */
async function processBatch(records, send, options) {
  const opts = options == null ? {} : options;
  const concurrency = opts.concurrency == null ? DEFAULT_CONCURRENCY : opts.concurrency;

  return mapLimit(
    records,
    concurrency,
    async (record) => ({id: record.id, value: await send(record)}),
    {signal: opts.signal}
  );
}

module.exports = {processBatch};
