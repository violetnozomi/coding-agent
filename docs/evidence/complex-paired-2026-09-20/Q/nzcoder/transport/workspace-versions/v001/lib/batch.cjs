'use strict';

const {mapLimit} = require('./pool.cjs');

/**
 * Send every record through `send` with bounded concurrency, resolving to the
 * `{id, value}` results in input order.
 *
 * - `options.concurrency` defaults to 2 and is forwarded to `mapLimit`.
 * - `options.signal` is forwarded to `mapLimit`; `send(record, index, signal)`
 *   also receives the signal so cooperative senders can stop early.
 * - Each record is sent at most once, and the first rejection (or abort reason)
 *   is propagated to the caller.
 *
 * @param {Array<{id: any}>} records
 * @param {(record: any, index: number, signal?: AbortSignal) => any} send
 * @param {{concurrency?: number, signal?: AbortSignal}} [options]
 * @returns {Promise<Array<{id: any, value: any}>>}
 */
function processBatch(records, send, options = {}) {
  const opts = options == null ? {} : options;
  const concurrency = opts.concurrency == null ? 2 : opts.concurrency;
  const signal = opts.signal;

  return mapLimit(
    records,
    concurrency,
    (record, index, workerSignal) =>
      Promise.resolve(send(record, index, workerSignal)).then(value => ({
        id: record.id,
        value
      })),
    {signal}
  );
}

module.exports = {processBatch};
