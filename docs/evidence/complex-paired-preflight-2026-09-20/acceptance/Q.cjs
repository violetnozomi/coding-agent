const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const {mapLimit, processBatch} = require(path.join(process.env.TASK_WORKSPACE, 'index.cjs'));
const deferred = () => { let resolve, reject; const promise=new Promise((a,b)=>{resolve=a;reject=b;}); return {promise,resolve,reject}; };
// Event-loop turns flush already queued completions; no elapsed-time assumptions.
const tick = () => new Promise(resolve=>setImmediate(resolve));

test('fills capacity, refills freed slots and preserves order and ownership', {timeout:2000}, async () => {
  const d=Array.from({length:4},deferred), started=[], items=[0,1,2,3];
  const result=mapLimit(items,2,(x,i)=>{assert.equal(x,i);started.push(i);return d[i].promise;});
  result.catch(()=>{});
  try {
    await tick();assert.deepEqual(started,[0,1]);
    d[1].resolve(false);await tick();assert.deepEqual(started,[0,1,2]);
    d[2].resolve(0);await tick();assert.deepEqual(started,[0,1,2,3]);
    d[3].resolve('');d[0].resolve(null);
    assert.deepEqual(await result,[null,false,0,'']);assert.deepEqual(items,[0,1,2,3]);
  } finally { d.forEach(x=>x.resolve(null)); await result.catch(()=>{}); }
});

test('first failure stops scheduling and drains secondary failures', {timeout:2000}, async () => {
  const d=[deferred(),deferred()], started=[], first=new Error('first'), second=new Error('second');
  let settled=false;
  const p=mapLimit([0,1,2],2,x=>{started.push(x);return d[x].promise;});
  const observed=p.then(()=>({ok:true}),e=>({error:e})).finally(()=>{settled=true;});
  try {
    await tick();assert.deepEqual(started,[0,1]);d[0].reject(first);
    await tick();assert.equal(settled,false);assert.deepEqual(started,[0,1]);
    d[1].reject(second);assert.equal((await observed).error,first);
  } finally { d.forEach(x=>x.resolve(null)); await observed; }
});

test('abort drains in-flight, forwards signal and removes listeners', {timeout:2000}, async () => {
  const c=new AbortController(), reason={cancelled:'caller'}, d=deferred(), started=[];
  let listeners=0, settled=false;
  const add=c.signal.addEventListener.bind(c.signal), remove=c.signal.removeEventListener.bind(c.signal);
  c.signal.addEventListener=(type,...args)=>{if(type==='abort')listeners++;return add(type,...args);};
  c.signal.removeEventListener=(type,...args)=>{if(type==='abort')listeners--;return remove(type,...args);};
  const p=mapLimit([0,1],1,(x,i,s)=>{assert.equal(s,c.signal);started.push(x);return d.promise;},{signal:c.signal});
  const observed=p.then(()=>({ok:true}),e=>({error:e})).finally(()=>{settled=true;});
  try {
    await tick();c.abort(reason);await tick();assert.equal(settled,false);assert.deepEqual(started,[0]);
    d.resolve('done');assert.equal((await observed).error,reason);assert.equal(listeners,0);
  } finally { d.resolve(null);await observed; }
});

test('pre-abort, empty validation and sync throw identity', async () => {
  const c=new AbortController(), reason={stopped:true};c.abort(reason);
  await assert.rejects(async()=>mapLimit([],2,()=>assert.fail('must not start'),{signal:c.signal}),e=>e===reason);
  for(const n of [0,-1,1.5,NaN,true])await assert.rejects(async()=>mapLimit([],n,x=>x),TypeError);
  await assert.rejects(async()=>mapLimit({},1,x=>x),TypeError);
  await assert.rejects(async()=>mapLimit([],1,null),TypeError);
  assert.deepEqual(await mapLimit([],1,x=>x),[]);
  const err=new Error('sync');
  await assert.rejects(async()=>mapLimit([1],1,()=>{throw err;}),e=>e===err);
});

test('first observed failure wins over later abort', {timeout:2000}, async () => {
  const c=new AbortController(),d=[deferred(),deferred()],err=new Error('worker first');
  const p=mapLimit([0,1,2],2,x=>d[x].promise,{signal:c.signal});
  const observed=p.then(()=>null,e=>e);
  try { await tick();d[0].reject(err);await tick();c.abort(new Error('later'));d[1].resolve(1);assert.equal(await observed,err); }
  finally {d.forEach(x=>x.resolve(null));await observed;}
});

test('batch forwards concurrency and abort', {timeout:2000}, async () => {
  const d=[deferred(),deferred(),deferred()],started=[];
  const p=processBatch([{id:0},{id:1},{id:2}],x=>{started.push(x.id);return d[x.id].promise;},{concurrency:3});
  p.catch(()=>{});
  try {await tick();assert.deepEqual(started,[0,1,2]);d.forEach((x,i)=>x.resolve(i*2));assert.deepEqual(await p,[{id:0,value:0},{id:1,value:2},{id:2,value:4}]);}
  finally {d.forEach(x=>x.resolve(null));await p.catch(()=>{});}
  const c=new AbortController();c.abort('stop');
  await assert.rejects(async()=>processBatch([{id:0}],()=>assert.fail('send after abort'),{signal:c.signal}),e=>e==='stop');
});

test('documents drain and cancellation', () => {
  const text=fs.readFileSync(path.join(process.env.TASK_WORKSPACE,'README.md'),'utf8').toLowerCase();
  assert.ok(text.includes('abort') && text.includes('concurr') && (text.includes('drain') || text.includes('in-flight')));
});
