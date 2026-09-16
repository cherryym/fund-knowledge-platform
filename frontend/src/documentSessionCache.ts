/** Bounded, session-memory metadata only. Never persists document bodies or credentials. */
export const DOCUMENT_CACHE_TTL = 30_000;
const LIMIT = 12;
type Entry<T> = { data?: T; error?: Error; updatedAt: number; token: string;
  promise?: Promise<T>; controller?: AbortController; sequence: number };
const entries = new Map<string, Entry<unknown>>();
const views = new Map<string, unknown>();
const resetListeners = new Set<() => void>();
const subscribers = new Map<string, number>();
let generation = 0;
export const documentCacheGeneration = () => generation;
export function subscribeDocumentCacheReset(listener: () => void) {
  resetListeners.add(listener);
  return () => { resetListeners.delete(listener); };
}
export function clearDocumentSessionCache() {
  generation++;
  for (const entry of entries.values()) entry.controller?.abort();
  entries.clear(); views.clear(); subscribers.clear();
  for (const listener of resetListeners) listener();
}
export function readDocumentEntry<T>(key: string): Entry<T> | undefined {
  const entry = entries.get(key);
  if (entry) { entries.delete(key); entries.set(key, entry); }
  return entry as Entry<T> | undefined;
}
/** Abort unused pagination on route exit, without cancelling another mounted consumer. */
export function retainDocumentRequest(key: string) {
  const epoch = generation;
  subscribers.set(key, (subscribers.get(key) ?? 0) + 1);
  let released = false;
  return () => {
    if (released || epoch !== generation) return;
    released = true;
    const remaining = Math.max(0, (subscribers.get(key) ?? 1) - 1);
    if (remaining) { subscribers.set(key, remaining); return; }
    subscribers.delete(key);
    const entry = entries.get(key);
    if (entry?.controller) {
      entry.sequence++;
      entry.controller.abort();
      entry.controller = undefined;
      entry.promise = undefined;
    }
  };
}
export function documentEntryFresh(entry: Entry<unknown> | undefined, token: string) {
  const age = Date.now() - (entry?.updatedAt ?? 0);
  return !!entry && entry.data !== undefined && !entry.error && entry.token === token
    && age >= 0 && age < DOCUMENT_CACHE_TTL;
}
export function loadDocumentEntry<T>(key: string, token: string,
  loader: (signal: AbortSignal) => Promise<T>, force = false): Promise<T> {
  let entry = readDocumentEntry<T>(key);
  if (!force && documentEntryFresh(entry, token)) return Promise.resolve(entry!.data!);
  if (!force && entry?.promise && entry.token === token) return entry.promise;
  if (!entry) {
    entry = {updatedAt: 0, token, sequence: 0};
    entries.set(key, entry);
    while (entries.size > LIMIT) {
      const oldest = entries.keys().next().value!;
      entries.get(oldest)?.controller?.abort(); entries.delete(oldest);
    }
  }
  const target = entry;
  target.controller?.abort();
  const controller = new AbortController(), epoch = generation, sequence = ++target.sequence;
  target.controller = controller; target.token = token;
  const valid = () => generation === epoch && entries.get(key) === target
    && target.sequence === sequence && !controller.signal.aborted;
  const promise = Promise.resolve().then(() => loader(controller.signal)).then(data => {
    if (!valid()) throw new DOMException("Stale document request", "AbortError");
    target.data = data; target.error = undefined; target.updatedAt = Date.now();
    return data;
  }).catch(error => {
    if (valid()) {
      target.error = error instanceof Error ? error : new Error(String(error));
      target.updatedAt = 0;
      if ([401,403,404].includes(Number(error?.status))) target.data = undefined;
    }
    throw error;
  }).finally(() => {
    if (target.sequence === sequence) { target.promise = undefined; target.controller = undefined; }
  });
  target.promise = promise;
  return promise;
}
export function readDocumentView<T>(key: string): T | undefined { return views.get(key) as T | undefined; }
export function writeDocumentView<T>(key: string, value: T) {
  views.delete(key); views.set(key, value);
  while (views.size > 8) views.delete(views.keys().next().value!);
}
