# Local job queue

schedule(jobs, worker) and Queue.enqueue(job)/run(worker) accept jobs with id,
payload and optional maxAttempts. The worker receives only payload.
maxAttempts is a positive integer including the first attempt, default 1;
invalid values raise RangeError before enqueue/invoke.
Only errors with retryable === true retry. On failure append the retry at the
tail behind queued work, including jobs enqueued by the worker.
Fairness example: A fails once, B is queued: order A, B, A. Results remain A, B.
Success records: {id, ok:true, value, attempts}. Failure: {id, ok:false, error, attempts}.
Exhaustion retains the final error object and actual attempts; old jobs run once.
Use RetryableError for retryable failures. summarize reports summed attempts.
Run node --test tests/queue.test.cjs tests/scheduler.test.cjs.
