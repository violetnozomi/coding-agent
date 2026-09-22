# Local job queue

schedule(jobs, worker) invokes a worker with each payload.
Jobs have id and payload. Results preserve submission order and contain
id, ok, value (success) or error (failure), and attempts.
Queue.enqueue and Queue.run support work added during execution.
