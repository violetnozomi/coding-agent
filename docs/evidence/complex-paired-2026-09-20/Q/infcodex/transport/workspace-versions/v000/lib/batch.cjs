const {mapLimit} = require('./pool.cjs');
async function processBatch(records, send, options = {}) {
  return mapLimit(records, 2, async record => ({id: record.id, value: await send(record)}));
}
module.exports = {processBatch};
