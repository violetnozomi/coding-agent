class RetryableError extends Error {
  constructor(message) { super(message); this.retryable = true; }
}
module.exports = { RetryableError };
