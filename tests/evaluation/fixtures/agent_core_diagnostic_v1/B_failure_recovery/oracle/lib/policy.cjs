function maxAttempts(job) {
  const limit = job.maxAttempts === undefined ? 1 : job.maxAttempts;
  if (!Number.isInteger(limit) || limit < 1) throw new RangeError('maxAttempts must be a positive integer');
  return limit;
}
function retry(result, limit) {
  return !result.ok && result.error?.retryable === true && result.attempts < limit;
}
module.exports = { maxAttempts, retry };
