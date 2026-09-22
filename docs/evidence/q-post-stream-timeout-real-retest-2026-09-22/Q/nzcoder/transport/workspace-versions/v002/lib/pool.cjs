'use strict';

/**
 * Bounded-concurrency map with stable ordering, failure draining and
 * AbortSignal cancellation.
 *
 * @param {Array} items input items (never mutated)
 * @param {number} limit positive integer, max workers in flight
 * @param {(item: any, index: number, signal: AbortSignal|undefined) => any} worker
 * @param {{signal?: AbortSignal}} [options]
 * @returns {Promise<Array>} results in input order
 */
function validateArguments(items, limit, worker) {
  if (!Array.isArray(items)) {
    throw new TypeError('mapLimit: items must be an array');
  }
  if (!Number.isInteger(limit) || limit <= 0) {
    throw new TypeError('mapLimit: limit must be a positive integer');
  }
  if (typeof worker !== 'function') {
    throw new TypeError('mapLimit: worker must be a function');
  }
}

function readSignal(options) {
  if (options === null || typeof options !== 'object') return undefined;
  return options.signal;
}

async function mapLimit(items, limit, worker, options = {}) {
  // Validate before any worker can run, including for empty input.
  validateArguments(items, limit, worker);

  const signal = readSignal(options);
  const total = items.length;

  // Abort wins over "empty input resolves []".
  if (signal && signal.aborted) {
    // Preserve identity of the reason, including falsy/non-Error values.
    throw signal.reason;
  }

  if (total === 0) return [];

  const results = new Array(total);

  return new Promise((resolve, reject) => {
    let nextIndex = 0;
    let inFlight = 0;
    let finished = false;
    let failed = false;
    let failureReason;

    const canListen = Boolean(signal) && typeof signal.addEventListener === 'function';

    function onAbort() {
      if (finished || failed) return;
      // First observed failure/abort wins.
      failed = true;
      failureReason = signal.reason;
      // Stop scheduling; in-flight work is drained before settling.
      schedule();
    }

    const cleanup = () => {
      if (canListen) signal.removeEventListener('abort', onAbort);
    };

    const finish = () => {
      if (finished) return;
      finished = true;
      cleanup();
      if (failed) reject(failureReason);
      else resolve(results);
    };

    const recordFailure = (reason) => {
      if (failed) return;
      failed = true;
      failureReason = reason;
    };

    function schedule() {
      if (finished) return;
      // Fill every available slot; never exceed `limit`.
      while (!failed && inFlight < limit && nextIndex < total) {
        start(nextIndex++);
      }
      if (failed) {
        if (inFlight === 0) finish();
        return;
      }
      if (nextIndex >= total && inFlight === 0) finish();
    }

    function start(index) {
      inFlight += 1;
      let outcome;
      try {
        outcome = worker(items[index], index, signal);
      } catch (error) {
        settle(index, error, true);
        return;
      }
      // Handlers are attached immediately so secondary failures can never
      // surface as unhandled rejections.
      Promise.resolve(outcome).then(
        (value) => settle(index, value, false),
        (error) => settle(index, error, true),
      );
    }

    function settle(index, value, isFailure) {
      inFlight -= 1;
      if (isFailure) recordFailure(value);
      else results[index] = value;
      schedule();
    }

    if (canListen) signal.addEventListener('abort', onAbort);
    schedule();
  });
}

module.exports = {mapLimit};
