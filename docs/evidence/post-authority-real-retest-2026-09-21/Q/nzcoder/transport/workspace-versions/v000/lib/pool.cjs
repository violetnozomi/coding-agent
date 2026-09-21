async function mapLimit(items, limit, worker, options = {}) {
  const results = [];
  for (let i = 0; i < items.length; i++) results.push(await worker(items[i], i));
  return results;
}
module.exports = {mapLimit};
