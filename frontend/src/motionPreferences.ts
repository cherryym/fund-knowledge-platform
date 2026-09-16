import { useSyncExternalStore } from "react";

export type MotionLevel = "rich" | "balanced" | "quiet";
export type MotionPreferences = {
  level: MotionLevel;
  effective: MotionLevel;
  reduced: boolean;
  setLevel: (level: MotionLevel) => void;
};

// UI preference only: no route, space content, model settings or credentials.
export const MOTION_PREFERENCE_KEY = "fkb:ui:motion-level";
const isLevel = (value: unknown): value is MotionLevel =>
  value === "rich" || value === "balanced" || value === "quiet";
const readLevel = (value: unknown): MotionLevel =>
  isLevel(value) ? value : "rich";
const listeners = new Set<() => void>();
let ownerWindow: Window | undefined;
let media: MediaQueryList | undefined;
let detach: (() => void) | undefined;
let volatileChoice = false;
let snapshot: MotionPreferences = {
  level: "rich",
  effective: "rich",
  reduced: false,
  setLevel,
};
const serverSnapshot = snapshot;

function update(level: MotionLevel, reduced: boolean) {
  if (snapshot.level === level && snapshot.reduced === reduced) return;
  snapshot = { level, effective: reduced ? "quiet" : level, reduced, setLevel };
  for (const listener of listeners) listener();
}
function initialize() {
  if (typeof window === "undefined" || ownerWindow === window) return;
  detach?.();
  detach = undefined;
  ownerWindow = window;
  volatileChoice = false;
  let level: MotionLevel = "rich";
  try {
    level = readLevel(window.localStorage.getItem(MOTION_PREFERENCE_KEY));
  } catch {
    /* Storage denial keeps the in-memory default. */
  }
  media = window.matchMedia("(prefers-reduced-motion: reduce)");
  snapshot = {
    level,
    effective: media.matches ? "quiet" : level,
    reduced: media.matches,
    setLevel,
  };
}
function setLevel(level: MotionLevel) {
  if (!isLevel(level)) return;
  initialize();
  update(level, media?.matches ?? false);
  try {
    ownerWindow?.localStorage.setItem(MOTION_PREFERENCE_KEY, level);
    volatileChoice = false;
  } catch {
    volatileChoice = true;
  }
}
function getSnapshot() {
  initialize();
  return snapshot;
}
function subscribe(listener: () => void) {
  initialize();
  listeners.add(listener);
  if (!detach && ownerWindow && media) {
    const currentWindow = ownerWindow;
    const currentMedia = media;
    const onSystemChange = () => update(snapshot.level, currentMedia.matches);
    const onStorage = (event: StorageEvent) => {
      if (event.key !== MOTION_PREFERENCE_KEY && event.key !== null) return;
      volatileChoice = false;
      update(
        readLevel(event.key === null ? null : event.newValue),
        currentMedia.matches,
      );
    };
    currentWindow.addEventListener("storage", onStorage);
    currentMedia.addEventListener("change", onSystemChange);
    detach = () => {
      currentWindow.removeEventListener("storage", onStorage);
      currentMedia.removeEventListener("change", onSystemChange);
    };
    // Catch storage/system changes that happened while the last consumer was unmounted.
    if (!volatileChoice) {
      try {
        update(
          readLevel(currentWindow.localStorage.getItem(MOTION_PREFERENCE_KEY)),
          currentMedia.matches,
        );
      } catch {
        onSystemChange();
      }
    } else onSystemChange();
  }
  return () => {
    listeners.delete(listener);
    if (!listeners.size) {
      detach?.();
      detach = undefined;
    }
  };
}

/** Shared same-page/cross-tab motion settings; system reduction always wins. */
export function useMotionPreferences(): MotionPreferences {
  return useSyncExternalStore(subscribe, getSnapshot, () => serverSnapshot);
}
