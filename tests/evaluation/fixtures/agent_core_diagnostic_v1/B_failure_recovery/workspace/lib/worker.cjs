async function invoke(worker, job) {
  try { return { id: job.id, ok: true, value: await worker(job.payload), attempts: 1 }; }
  catch (error) { return { id: job.id, ok: false, error, attempts: 1 }; }
}
module.exports = { invoke };
