'use strict';

/**
 * Run `worker` over `items` with at most `limit` workers in flight, resolving to
 * the worker results in input order.
 *
 * Contract
 * --------
 * - `items` must be an array, `limit` a positive integer and `worker` callable.
 *   Invalid arguments reject with a `TypeError` *before* `worker` is invoked
 *   (validation also happens for empty input).
 * - `worker(item, index, signal)` may return a value, a promise, or throw
 *   synchronously. `items` is never mutated.
 * - At most `limit` workers run concurrently, and free slots are refilled as
 *   soon as a worker settles (no waiting for the whole batch). Every item is
 *   started exactly once.
 * - Once a worker failure (or an abort) is observed no new item is started; the
 *   already-started workers are allowed to settle and the first observed
 *   outcome wins. Rejections preserve the original error/reason identity.
 * - Rejection handlers are attached to every started worker, so secondary
 *   failures never surface as unhandled rejections.
 * - If `options.signal` is already aborted, no work starts and the returned
 *   promise rejects with `signal.reason`. If it aborts mid-run, scheduling
 *   stops, in-flight workers drain, and the promise then rejects with that
 *   reason. The installed abort listener is removed after settlement.
 *
 * @param {Array} items
 * @param {number} limit positive integer concurrency
 * @param {(item: any, index: number, signal?: AbortSignal) => any} worker
 * @param {{signal?: AbortSignal}} [options]
 * @returns {Promise<Array>} results in input order
 */
function mapLimit(items, limit, worker, options = {}) {
  const signal = options == null ? undefined : options.signal;

  if (!Array.isArray(items)) {
    return Promise.reject(new TypeError('mapLimit: items must be an array'));
  }
  if (!Number.isInteger(limit) || limit <= 0) {
    return Promise.reject(new TypeError('mapLimit: limit must be a positive integer'));
  }
  if (typeof worker !== 'function') {
    return Promise.reject(new TypeError('mapLimit: worker must be a function'));
  }

  // Already-aborted signals start no work at all, even for empty input.
  if (signal != null && signal.aborted) {
    return Promise.reject(signal.reason);
  }

  return new Promise((resolve, reject) => {
    const length = items.length;
    const results = new Array(length);
    let nextIndex = 0;
    let inFlight = 0;
    let settled = false;
    let hasOutcome = false;
    let outcome;

    function cleanup() {
      if (signal != null && typeof signal.removeEventListener === 'function') {
        signal.removeEventListener('abort', onAbort);
      }
    }

    // Settle only once nothing is in flight; drain before rejecting.
    function settle() {
      if (settled || inFlight > 0) return;
      if (hasOutcome) {
        settled = true;
        cleanup();
        reject(outcome);
      } else if (nextIndex >= length) {
        settled = true;
        cleanup();
        resolve(results);
      }
    }

    // First observed failure/abort wins; later ones are ignored.
    function recordOutcome(value) {
      if (!hasOutcome) {
        hasOutcome = true;
        outcome = value;
      }
    }

    function onAbort() {
      recordOutcome(signal.reason);
      settle();
    }

    function schedule() {
      while (!settled && !hasOutcome && inFlight < limit && nextIndex < length) {
        const index = nextIndex;
        nextIndex += 1;
        inFlight += 1;

        let value;
        try {
          value = worker(items[index], index, signal);
        } catch (error) {
          // Synchronous throw is an observed failure: stop starting new work.
          inFlight -= 1;
          recordOutcome(error);
          break;
        }

        Promise.resolve(value).then(
          result => {
            results[index] = result;
            inFlight -= 1;
            settle();
            schedule();
          },
          error => {
            inFlight -= 1;
            recordOutcome(error);
            settle();
            schedule();
          }
        );
      }
      settle();
    }

    if (signal != null && typeof signal.addEventListener === 'function') {
      signal.addEventListener('abort', onAbort);
    }

    schedule();
  });
}

module.exports = {mapLimit};
