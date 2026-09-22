function summarize(results) {
  return { succeeded: results.filter(x => x.ok).length,
    failed: results.filter(x => !x.ok).length,
    attempts: results.reduce((n, x) => n + x.attempts, 0) };
}
module.exports = { summarize };
