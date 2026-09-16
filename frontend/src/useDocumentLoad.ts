import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { DOCUMENT_CACHE_TTL, documentCacheGeneration, documentEntryFresh, loadDocumentEntry,
  readDocumentEntry, retainDocumentRequest, subscribeDocumentCacheReset } from "./documentSessionCache";

/** Opt-in only for document list/taxonomy metadata; all other readers remain uncached. */
export function useDocumentLoad<T>(key: string | null, loader: (signal: AbortSignal) => Promise<T>, dependencies: unknown[]) {
  const generation = useSyncExternalStore(subscribeDocumentCacheReset, documentCacheGeneration, documentCacheGeneration);
  const token = JSON.stringify(dependencies), identity = `${generation}:${key ?? token}`;
  const [nonce, setNonce] = useState(0), seenNonce = useRef(0);
  type State = {identity: string; data?: T; loading: boolean; refreshing: boolean; error?: Error};
  const cachedState = (): State => {
    const cached = key ? readDocumentEntry<T>(key) : undefined;
    return {identity, data: cached?.data, loading: cached?.data === undefined, refreshing: false, error: cached?.error};
  };
  const [state, setState] = useState<State>(cachedState);
  useEffect(() => {
    let active = true;
    const release = key ? retainDocumentRequest(key) : undefined;
    const forced = nonce !== seenNonce.current; seenNonce.current = nonce;
    const controller = new AbortController();
    const read = async (force = false) => {
      const cached = key ? readDocumentEntry<T>(key) : undefined;
      if (key && !force && documentEntryFresh(cached, token)) {
        if (active) setState({identity, data: cached!.data, loading: false, refreshing: false});
        return;
      }
      if (active) setState({identity, data: cached?.data, loading: cached?.data === undefined, refreshing: true});
      try {
        const data = key ? await loadDocumentEntry(key, token, loader, force) : await loader(controller.signal);
        if (active) setState({identity, data, loading: false, refreshing: false});
      } catch (error) {
        if (active && !controller.signal.aborted) setState({identity, data: key ? readDocumentEntry<T>(key)?.data : undefined,
          loading: false, refreshing: false, error: error instanceof Error ? error : new Error(String(error))});
      }
    };
    void read(forced);
    const check = () => {
      if (key && document.visibilityState === "visible" && !documentEntryFresh(readDocumentEntry(key), token)) void read();
    };
    const timer = key ? setInterval(check, DOCUMENT_CACHE_TTL) : undefined;
    if (key) { window.addEventListener("focus", check); document.addEventListener("visibilitychange", check); }
    return () => {
      active = false; controller.abort(); release?.();
      if (timer) clearInterval(timer);
      window.removeEventListener("focus", check); document.removeEventListener("visibilitychange", check);
    };
    // The key and token describe the complete scope/query; a new loader closure alone is not a reload.
  }, [identity, token, nonce]);
  return {...(state.identity === identity ? state : cachedState()), reload: () => setNonce(value => value + 1)};
}
