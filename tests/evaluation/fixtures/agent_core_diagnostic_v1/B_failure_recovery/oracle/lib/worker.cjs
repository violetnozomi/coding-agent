async function invoke(worker, job, attempts = 1) {
  try { return { id: job.id, ok: true, value: await worker(job.payload), attempts }; }
  catch (error) { return { id: job.id, ok: false, error, attempts }; }
}
module.exports = { invoke };
