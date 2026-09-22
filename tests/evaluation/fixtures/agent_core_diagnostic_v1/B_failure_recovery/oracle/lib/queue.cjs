const { invoke } = require('./worker.cjs');
const { maxAttempts, retry } = require('./policy.cjs');
class Queue {
  constructor() { this.pending = []; this.count = 0; }
  enqueue(job) {
    const limit = maxAttempts(job);
    this.pending.push({ job, limit, attempts: 0, index: this.count++ });
  }
  async run(worker) {
    const results = [];
    while (this.pending.length) {
      const entry = this.pending.shift();
      const result = await invoke(worker, entry.job, ++entry.attempts);
      if (retry(result, entry.limit)) this.pending.push(entry);
      else results[entry.index] = result;
    }
    return results;
  }
}
module.exports = { Queue };
