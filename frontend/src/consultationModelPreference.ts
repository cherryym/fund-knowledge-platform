import { useCallback, useMemo, useSyncExternalStore } from "react";
import type { ModelSelection } from "./models.types";

const changedEvent = "fund-kb:consultation-model-preference";
const volatileSelections = new Map<string, string | null>();

export const consultationModelPreferenceKey = (userId: string) =>
  `fund-kb:consultation-model:v1:${encodeURIComponent(userId)}`;

function parseSelection(raw: string | null): ModelSelection | null {
  if (!raw || raw.length > 4096) return null;
  try {
    const value = JSON.parse(raw);
    if (
      value?.version !== 1 ||
      typeof value.connection_id !== "string" || !value.connection_id.trim() ||
      typeof value.model_id !== "string" || !value.model_id.trim()
    ) return null;
    return { connection_id: value.connection_id, model_id: value.model_id };
  } catch { return null; }
}

function readSnapshot(key: string): string | null {
  if (volatileSelections.has(key)) return volatileSelections.get(key) ?? null;
  try { return window.localStorage.getItem(key); } catch { return null; }
}

/** Only identifiers are preferences. Options, permissions and credentials remain server-owned. */
export function useConsultationModelPreference(userId: string) {
  const key = consultationModelPreferenceKey(userId);
  const subscribe = useCallback((notify: () => void) => {
    const localChanged = (event: Event) => {
      if ((event as CustomEvent<{ key: string }>).detail?.key === key) notify();
    };
    const storageChanged = (event: StorageEvent) => {
      if (event.key !== null && event.key !== key) return;
      // A newer preference (or explicit clear) from another tab wins.
      volatileSelections.delete(key);
      notify();
    };
    window.addEventListener(changedEvent, localChanged);
    window.addEventListener("storage", storageChanged);
    return () => {
      window.removeEventListener(changedEvent, localChanged);
      window.removeEventListener("storage", storageChanged);
    };
  }, [key]);
  const getSnapshot = useCallback(() => readSnapshot(key), [key]);
  const raw = useSyncExternalStore(subscribe, getSnapshot, () => null);
  const selection = useMemo(() => parseSelection(raw), [raw]);
  const setSelection = useCallback((value: ModelSelection | null) => {
    const next = value ? JSON.stringify({
      version: 1, connection_id: value.connection_id, model_id: value.model_id,
    }) : null;
    try {
      if (next === null) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, next);
      volatileSelections.delete(key);
    } catch {
      // Restricted storage must not break selection or same-session navigation.
      volatileSelections.set(key, next);
    }
    window.dispatchEvent(new window.CustomEvent(changedEvent, { detail: { key } }));
  }, [key]);
  return [selection, setSelection] as const;
}
