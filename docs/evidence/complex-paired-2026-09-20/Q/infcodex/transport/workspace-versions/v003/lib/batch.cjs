'use strict';

const { mapLimit } = require('./pool.cjs');

/**
 * Send each record once, preserving input order.
 *
 * @param {Array<{id: any}>} records
 * @param {(record: any, signal?: AbortSignal) => any} send
 * @param {{concurrency?: number, signal?: AbortSignal}} [options]
 * @returns {Promise<Array<{id: any, value: any}>>}
 */
async function processBatch(records, send, options = {}) {
  const opts = options == null ? {} : options;
  const concurrency = opts.concurrency === undefined ? 2 : opts.concurrency;

  return mapLimit(
    records,
    concurrency,
    async (record, _index, signal) => ({
      id: record.id,
      value: await send(record, signal),
    }),
    { signal: opts.signal },
  );
}

module.exports = { processBatch };
