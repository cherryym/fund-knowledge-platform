/** Complete authorized Wiki snapshots, memory-only and scoped to one login.
 * This is a reader snapshot, never authority for editing, download or model use.
 */
export const WIKI_SNAPSHOT_TTL = 30_000;
const LIMIT = 4;
export type WikiEntry<T> = {data?: T; error?: Error; updatedAt: number; token: string;
  promise?: Promise<T>; controller?: AbortController; sequence: number};
const entries = new Map<string, WikiEntry<unknown>>();
const views = new Map<string, unknown>();
const listeners = new Set<() => void>();
let epoch = 0;
export const wikiCacheGeneration = () => epoch;
export function subscribeWikiCache(listener: () => void) { listeners.add(listener); return () => {listeners.delete(listener);}; }
export function clearWikiSessionCache() {
  epoch++;
  for (const entry of entries.values()) entry.controller?.abort();
  entries.clear(); views.clear();
  for (const listener of listeners) listener();
}
export function readWikiEntry<T>(key: string): WikiEntry<T> | undefined { return entries.get(key) as WikiEntry<T> | undefined; }
export function freshWikiEntry(entry: WikiEntry<unknown> | undefined, token: string) {
  const age = Date.now() - (entry?.updatedAt ?? 0);
  return !!entry && entry.data !== undefined && !entry.error && entry.token === token && age >= 0 && age < WIKI_SNAPSHOT_TTL;
}
export function loadWikiEntry<T>(key: string, token: string, loader: (signal: AbortSignal) => Promise<T>, force=false): Promise<T> {
  let entry = readWikiEntry<T>(key);
  if (!force && freshWikiEntry(entry,token)) return Promise.resolve(entry!.data!);
  if (!force && entry?.promise && entry.token === token) return entry.promise;
  if (!entry) {
    entry = {token,updatedAt:0,sequence:0}; entries.set(key,entry);
    while(entries.size > LIMIT) { const oldest=entries.keys().next().value!; entries.get(oldest)?.controller?.abort(); entries.delete(oldest); }
  }
  const target=entry, generation=epoch, sequence=++target.sequence;
  target.controller?.abort();
  const controller=new AbortController(); target.controller=controller;
  if (target.token !== token) { target.data=undefined; target.error=undefined; }
  target.token=token;
  const valid=()=>epoch===generation && entries.get(key)===target && target.sequence===sequence && !controller.signal.aborted;
  const promise=Promise.resolve().then(()=>loader(controller.signal)).then(data=>{
    if(!valid()) throw new DOMException('Stale Wiki snapshot','AbortError');
    target.data=data; target.error=undefined; target.updatedAt=Date.now(); return data;
  }).catch(error=>{
    if(valid()) {
      target.error=error instanceof Error?error:new Error(String(error)); target.updatedAt=0;
      if([401,403,404].includes(Number(error?.status))) target.data=undefined;
    }
    throw error;
  }).finally(()=>{ if(target.sequence===sequence) {target.promise=undefined;target.controller=undefined;} });
  target.promise=promise; return promise;
}
export function readWikiView<T>(key: string): T | undefined { return views.get(key) as T | undefined; }
export function writeWikiView<T>(key: string,value:T) {
  views.delete(key); views.set(key,value);
  while(views.size>4) views.delete(views.keys().next().value!);
}
