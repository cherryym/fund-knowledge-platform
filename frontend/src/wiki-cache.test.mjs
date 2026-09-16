import test, {afterEach} from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
const bundled=await build({entryPoints:['src/wikiSessionCache.ts'],bundle:true,write:false,format:'esm',platform:'node'});
const cache=await import(`data:text/javascript;base64,${Buffer.from(bundled.outputFiles[0].text).toString('base64')}`);
afterEach(()=>cache.clearWikiSessionCache());

test('concurrent and remounted readers share one complete request',async()=>{
  let calls=0,resolve;
  const loader=()=>{calls++;return new Promise(done=>{resolve=done;});};
  const a=cache.loadWikiEntry('user:space','1',loader),b=cache.loadWikiEntry('user:space','1',loader);
  await Promise.resolve();assert.equal(calls,1);assert.equal(a,b);
  const data={readers:{a:{body:'full one'},b:{body:'full two'}}};resolve(data);
  assert.equal(await a,data);assert.equal(await cache.loadWikiEntry('user:space','1',loader),data);
  assert.equal(calls,1);
});
test('different users and spaces never share snapshot bodies',async()=>{
  await cache.loadWikiEntry('alice:personal','1',async()=>({body:'alice'}));
  assert.equal(cache.readWikiEntry('bob:personal'),undefined);
  assert.equal(cache.readWikiEntry('alice:team'),undefined);
});
test('known content revision removes the old body before the replacement completes',async()=>{
  await cache.loadWikiEntry('k','1',async()=>({body:'old'}));
  let resolve;const pending=cache.loadWikiEntry('k','2',()=>new Promise(done=>resolve=done));
  assert.equal(cache.readWikiEntry('k').data,undefined);
  await Promise.resolve();resolve({body:'new'});await pending;
  assert.equal(cache.readWikiEntry('k').data.body,'new');
});
test('logout invalidates late in-flight data and view state',async()=>{
  let resolve;const pending=cache.loadWikiEntry('k','1',()=>new Promise(done=>resolve=done));
  await Promise.resolve();cache.writeWikiView('u:s',{selected:'a'});cache.clearWikiSessionCache();
  resolve({body:'must not reappear'});await assert.rejects(pending,{name:'AbortError'});
  assert.equal(cache.readWikiEntry('k'),undefined);assert.equal(cache.readWikiView('u:s'),undefined);
});
for(const status of [401,403,404])test(`permission failure ${status} removes cached body`,async()=>{
  await cache.loadWikiEntry('k','1',async()=>({body:'allowed earlier'}));
  await assert.rejects(cache.loadWikiEntry('k','1',async()=>{throw Object.assign(new Error('denied'),{status});},true));
  assert.equal(cache.readWikiEntry('k').data,undefined);
});
test('transient refresh failure is explicit and retains the previous reader snapshot',async()=>{
  const data={body:'previous snapshot'};await cache.loadWikiEntry('k','1',async()=>data);
  await assert.rejects(cache.loadWikiEntry('k','1',async()=>{throw Object.assign(new Error('offline'),{status:0});},true));
  const entry=cache.readWikiEntry('k');assert.equal(entry.data,data);assert.equal(entry.error.message,'offline');
  assert.equal(cache.freshWikiEntry(entry,'1'),false);
});
test('expiration and token changes require fresh validation without truncating data',async()=>{
  const data={pages:Array.from({length:1200},(_,i)=>i)};await cache.loadWikiEntry('k','1',async()=>data);
  const entry=cache.readWikiEntry('k');assert.equal(entry.data.pages.length,1200);
  assert.equal(cache.freshWikiEntry(entry,'1'),true);assert.equal(cache.freshWikiEntry(entry,'2'),false);
  entry.updatedAt-=cache.WIKI_SNAPSHOT_TTL+1;assert.equal(cache.freshWikiEntry(entry,'1'),false);
});
