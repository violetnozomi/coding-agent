'use strict';

const {mapLimit} = require('./pool.cjs');

const DEFAULT_CONCURRENCY = 2;

/**
 * Sends every record at most once with bounded concurrency and returns
 * `{id, value}` records in input order.
 *
 * @param {Array<{id: any}>} records
 * @param {(record: any, index: number, signal: AbortSignal|undefined) => any} send
 * @param {{concurrency?: number, signal?: AbortSignal}} [options]
 * @returns {Promise<Array<{id: any, value: any}>>}
 */
function processBatch(records, send, options = {}) {
  const settings = options === null || typeof options !== 'object' ? {} : options;
  const concurrency =
    settings.concurrency === undefined ? DEFAULT_CONCURRENCY : settings.concurrency;

  return mapLimit(
    records,
    concurrency,
    async (record, index, signal) => ({id: record.id, value: await send(record, index, signal)}),
    {signal: settings.signal},
  );
}

module.exports = {processBatch};
