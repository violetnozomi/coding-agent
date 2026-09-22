const { invoke } = require('./worker.cjs');
class Queue {
  constructor() { this.pending = []; this.count = 0; }
  enqueue(job) { this.pending.push({ job, index: this.count++ }); }
  async run(worker) {
    const results = [];
    while (this.pending.length) {
      const { job, index } = this.pending.shift();
      results[index] = await invoke(worker, job);
    }
    return results;
  }
}
module.exports = { Queue };
