'use strict';

/**
 * Bounded-concurrency map.
 *
 * mapLimit(items, limit, worker, {signal} = {}) -> Promise<results>
 *
 * Guarantees:
 *  - arguments are validated (TypeError) before any worker is invoked,
 *    including for empty input;
 *  - results resolve in input order and the input array is never mutated;
 *  - at most `limit` workers are in flight, available slots are filled
 *    immediately and every item is started exactly once;
 *  - the first observed worker failure stops scheduling; already-started
 *    work is allowed to drain and then the first failure is rethrown with
 *    its original identity;
 *  - an optional AbortSignal cancels pending work, preserves the abort
 *    reason identity and its listener is removed once settled.
 */
function mapLimit(items, limit, worker, options) {
  return new Promise((resolve, reject) => {
    if (!Array.isArray(items)) {
      reject(new TypeError('mapLimit: items must be an array'));
      return;
    }
    if (!Number.isInteger(limit) || limit <= 0) {
      reject(new TypeError('mapLimit: limit must be a positive integer'));
      return;
    }
    if (typeof worker !== 'function') {
      reject(new TypeError('mapLimit: worker must be a function'));
      return;
    }

    const opts = options == null ? {} : options;
    const signal = opts.signal;
    const canListen = signal != null && typeof signal.addEventListener === 'function';

    // An already-aborted signal starts no work at all.
    if (signal != null && signal.aborted) {
      reject(signal.reason);
      return;
    }

    const total = items.length;
    const results = new Array(total);

    let nextIndex = 0;
    let active = 0;
    let failureObserved = false;
    let failureReason;
    let settled = false;

    function removeAbortListener() {
      if (canListen) signal.removeEventListener('abort', onAbort);
    }

    function observeFailure(reason) {
      if (failureObserved) return;
      failureObserved = true;
      failureReason = reason;
    }

    function finish() {
      if (settled) return;
      if (failureObserved) {
        // Wait for every already-started worker to settle before rejecting.
        if (active > 0) return;
        settled = true;
        removeAbortListener();
        reject(failureReason);
        return;
      }
      if (active === 0 && nextIndex >= total) {
        settled = true;
        removeAbortListener();
        resolve(results);
      }
    }

    function onSlotSettled() {
      active -= 1;
      if (failureObserved) finish();
      else schedule();
    }

    function onAbort() {
      // First observed failure/abort wins; in-flight work still drains.
      observeFailure(signal.reason);
      finish();
    }

    function schedule() {
      if (settled) return;
      while (!failureObserved && active < limit && nextIndex < total) {
        const index = nextIndex;
        nextIndex += 1;
        active += 1;

        let produced;
        try {
          produced = worker(items[index], index, signal);
        } catch (error) {
          active -= 1;
          observeFailure(error);
          finish();
          return;
        }

        // Both handlers are attached so secondary failures stay handled.
        Promise.resolve(produced).then(
          (value) => {
            results[index] = value;
            onSlotSettled();
          },
          (error) => {
            observeFailure(error);
            onSlotSettled();
          }
        );
      }
      finish();
    }

    if (canListen) signal.addEventListener('abort', onAbort);
    schedule();
  });
}

module.exports = {mapLimit};
