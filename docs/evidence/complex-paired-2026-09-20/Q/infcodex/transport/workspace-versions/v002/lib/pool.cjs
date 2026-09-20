'use strict';

/**
 * Run `worker` over `items` with at most `limit` invocations in flight.
 *
 * Contract:
 *   - `items` must be an array, `limit` a positive integer, `worker` callable.
 *     Invalid arguments reject with a TypeError before any worker is invoked
 *     (validation happens even for empty input).
 *   - `items` is never mutated.
 *   - `worker(item, index, signal)` may return a value or a Promise, and may
 *     throw synchronously. Its result is written to `results[index]` verbatim,
 *     so falsy results (0, '', false, null, undefined) are preserved.
 *   - Results resolve in input order.
 *   - No more than `limit` workers run at once; free slots are filled eagerly
 *     and every item is started exactly once.
 *   - The first observed worker failure stops new scheduling; already-started
 *     workers are allowed to settle, then the returned promise rejects with the
 *     first failure (object identity preserved). Secondary rejections have
 *     handlers attached and never surface as unhandled rejections.
 *   - Cancellation via `options.signal` (an AbortSignal): if already aborted, no
 *     work starts and the promise rejects with `signal.reason` (identity
 *     preserved, including non-Error reasons). If aborted mid-run, scheduling
 *     stops, in-flight workers are drained, then the promise rejects with the
 *     abort reason. The first observed failure/abort wins. Abort listeners are
 *     removed once the promise settles. An uncooperative worker is never
 *     assumed to have stopped — the pool waits for it to actually settle.
 *
 * @param {Array} items
 * @param {number} limit
 * @param {(item: any, index: number, signal?: AbortSignal) => any} worker
 * @param {{signal?: AbortSignal}} [options]
 * @returns {Promise<Array>}
 */
async function mapLimit(items, limit, worker, options = {}) {
  if (!Array.isArray(items)) {
    throw new TypeError('mapLimit: items must be an array');
  }
  if (!Number.isInteger(limit) || limit <= 0) {
    throw new TypeError('mapLimit: limit must be a positive integer');
  }
  if (typeof worker !== 'function') {
    throw new TypeError('mapLimit: worker must be a function');
  }

  const opts = options == null ? {} : options;
  const signal = opts.signal == null ? undefined : opts.signal;
  if (signal !== undefined && typeof signal.addEventListener !== 'function') {
    throw new TypeError('mapLimit: options.signal must be an AbortSignal');
  }

  const total = items.length;
  const results = new Array(total);

  // An already-aborted signal starts no work and rejects with the exact reason.
  if (signal !== undefined && signal.aborted) {
    throw signal.reason;
  }

  // Empty input resolves [] when not aborted.
  if (total === 0) {
    return results;
  }

  let nextIndex = 0; // next item to start
  let inFlight = 0; // workers currently running
  let settled = false; // outer promise has resolved/rejected
  let hasFailure = false; // a failure or abort has been observed
  let failure; // first observed failure/abort reason (identity preserved)
  let listening = false;
  let resolveDone;
  let rejectDone;
  const done = new Promise((resolve, reject) => {
    resolveDone = resolve;
    rejectDone = reject;
  });

  function recordFailure(reason) {
    // First observed failure/abort wins.
    if (!hasFailure) {
      hasFailure = true;
      failure = reason;
    }
  }

  function cleanup() {
    if (listening && typeof signal.removeEventListener === 'function') {
      signal.removeEventListener('abort', onAbort);
    }
    listening = false;
  }

  function maybeFinish() {
    if (settled) return;
    if (inFlight > 0) return; // wait for already-started workers to settle
    if (hasFailure) {
      settled = true;
      cleanup();
      rejectDone(failure);
      return;
    }
    if (nextIndex >= total) {
      settled = true;
      cleanup();
      resolveDone(results);
    }
  }

  function onAbort() {
    if (settled) return;
    recordFailure(signal.reason);
    maybeFinish();
  }

  function startOne(index) {
    inFlight++;
    let ret;
    try {
      ret = worker(items[index], index, signal);
    } catch (err) {
      // Synchronous throw: record it and let the pump loop stop scheduling.
      recordFailure(err);
      inFlight--;
      return;
    }
    // Attach handlers synchronously so secondary failures are never unhandled.
    Promise.resolve(ret).then(
      (value) => {
        results[index] = value; // never overwrite falsy results
        inFlight--;
        pump();
      },
      (err) => {
        recordFailure(err);
        inFlight--;
        pump();
      },
    );
  }

  function pump() {
    while (!hasFailure && inFlight < limit && nextIndex < total) {
      startOne(nextIndex++);
    }
    maybeFinish();
  }

  if (signal !== undefined) {
    signal.addEventListener('abort', onAbort);
    listening = true;
    // The signal may have aborted between the initial check and here, in which
    // case the 'abort' event already fired and will not fire again.
    if (signal.aborted && !hasFailure) {
      recordFailure(signal.reason);
    }
  }

  pump();
  return done;
}

module.exports = { mapLimit };
