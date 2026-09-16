import test, {afterEach} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {transform} from 'esbuild';
const code=await transform(await readFile(new URL('./documentSessionCache.ts',import.meta.url),'utf8'),{loader:'ts',format:'esm'});
const cache=await import('data:text/javascript;base64,'+Buffer.from(code.code).toString('base64'));
const originalNow=Date.now;
afterEach(()=>{Date.now=originalNow;cache.clearDocumentSessionCache();});
const value={resources:[{id:'synthetic',name:'仅元数据'}]};

test('fresh metadata is reused and simultaneous mounts share one request',async()=>{
  let count=0,finish;
  const loader=()=>{count++;return new Promise(resolve=>{finish=resolve;});};
  const a=cache.loadDocumentEntry('user-a:space-a','revision-1',loader);
  const b=cache.loadDocumentEntry('user-a:space-a','revision-1',loader);
  assert.equal(a,b);await Promise.resolve();assert.equal(count,1);
  finish(value);assert.deepEqual(await a,value);
  assert.deepEqual(await cache.loadDocumentEntry('user-a:space-a','revision-1',loader),value);
  assert.equal(count,1);
});
test('expiry and refresh tokens revalidate while retaining previous metadata',async()=>{
  let now=1000;Date.now=()=>now;
  await cache.loadDocumentEntry('key','0',async()=>value);
  now=999;assert.equal(cache.documentEntryFresh(cache.readDocumentEntry('key'),'0'),false);
  now=1000;
  now+=cache.DOCUMENT_CACHE_TTL+1;
  assert.equal(cache.documentEntryFresh(cache.readDocumentEntry('key'),'0'),false);
  let finish;
  const pending=cache.loadDocumentEntry('key','0',()=>new Promise(resolve=>{finish=resolve;}));
  await Promise.resolve();assert.equal(cache.readDocumentEntry('key').data,value);
  finish({resources:[]});await pending;
  let count=0;
  await cache.loadDocumentEntry('key','1',async()=>{count++;return value;});
  assert.equal(count,1);
});
test('refresh cancels obsolete work and a late result cannot overwrite current data',async()=>{
  let firstFinish,firstSignal;
  const old=cache.loadDocumentEntry('key','0',signal=>{firstSignal=signal;return new Promise(resolve=>{firstFinish=resolve;});});
  old.catch(()=>{});await Promise.resolve();
  await cache.loadDocumentEntry('key','1',async()=>value);
  assert.equal(firstSignal.aborted,true);
  firstFinish({resources:[{id:'stale'}]});await assert.rejects(old,{name:'AbortError'});
  assert.equal(cache.readDocumentEntry('key').data,value);
});
test('logout clears data and views, aborts requests, and rejects late responses',async()=>{
  await cache.loadDocumentEntry('warm','0',async()=>value);
  cache.writeDocumentView('scope',{q:'合成查询',listScrollTop:100});
  let finish,signal;
  const pending=cache.loadDocumentEntry('pending','0',s=>{signal=s;return new Promise(resolve=>{finish=resolve;});});
  pending.catch(()=>{});await Promise.resolve();
  const previous=cache.documentCacheGeneration();cache.clearDocumentSessionCache();
  assert.equal(signal.aborted,true);assert.equal(cache.documentCacheGeneration(),previous+1);
  finish(value);await assert.rejects(pending,{name:'AbortError'});
  assert.equal(cache.readDocumentEntry('warm'),undefined);assert.equal(cache.readDocumentEntry('pending'),undefined);
  assert.equal(cache.readDocumentView('scope'),undefined);
});
test('user and library scopes never share data or UI state',async()=>{
  await cache.loadDocumentEntry('user-a:space-a','0',async()=>value);
  await cache.loadDocumentEntry('user-a:space-b','0',async()=>({resources:[]}));
  assert.equal(cache.readDocumentEntry('user-b:space-a'),undefined);
  assert.equal(cache.readDocumentEntry('user-a:space-b').data.resources.length,0);
  cache.writeDocumentView('user-a:space-a',{q:'private-query'});
  assert.equal(cache.readDocumentView('user-b:space-a'),undefined);
});
test('access denial clears cached metadata; transient network failures retain it with error',async()=>{
  await cache.loadDocumentEntry('key','0',async()=>value);
  await assert.rejects(cache.loadDocumentEntry('key','0',async()=>{throw Object.assign(new Error('offline'),{status:0});},true));
  assert.equal(cache.readDocumentEntry('key').data,value);
  assert.equal(cache.readDocumentEntry('key').error.message,'offline');
  await assert.rejects(cache.loadDocumentEntry('key','0',async()=>{throw Object.assign(new Error('denied'),{status:403});},true));
  assert.equal(cache.readDocumentEntry('key').data,undefined);
});
test('metadata and UI state have bounded LRU capacity',async()=>{
  for(let i=0;i<13;i++)await cache.loadDocumentEntry('key-'+i,'0',async()=>({i}));
  assert.equal(cache.readDocumentEntry('key-0'),undefined);
  assert.equal(cache.readDocumentEntry('key-12').data.i,12);
  for(let i=0;i<9;i++)cache.writeDocumentView('view-'+i,{i});
  assert.equal(cache.readDocumentView('view-0'),undefined);
});
