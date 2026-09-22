const { schedule, summarize } = require('./index.cjs');
schedule(process.argv.slice(2).map((payload, id) => ({id, payload})), x => x.toUpperCase())
  .then(results => console.log(JSON.stringify({results, metrics: summarize(results)})));
