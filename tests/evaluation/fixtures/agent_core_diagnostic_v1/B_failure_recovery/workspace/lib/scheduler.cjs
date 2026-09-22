const { Queue } = require('./queue.cjs');
async function schedule(jobs, worker) {
  const queue = new Queue();
  for (const job of jobs) queue.enqueue(job);
  return queue.run(worker);
}
module.exports = { schedule };
