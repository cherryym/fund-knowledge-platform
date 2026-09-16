import { useEffect, useState } from "react";
import { STAR_PHYSICS_LIMITS, STAR_DEFAULT_PHYSICS, normalizeStarPhysics, type StarPhysics } from "./starPhysicsSettings";
export { STAR_PHYSICS_LIMITS, STAR_DEFAULT_PHYSICS, normalizeStarPhysics, type StarPhysics } from "./starPhysicsSettings";

export const STAR_GROUPS = ["source", "wiki", "term", "template"] as const;
export type StarGroup = typeof STAR_GROUPS[number];
export type StarGraphTheme = {
  version: 1; mode: "group" | "unified"; unified: string; groups: Record<StarGroup, string>;
  edge: string; highlight: string; nodes: Record<string, string>; labels: "auto" | "all" | "none"; physics: StarPhysics;
};

export function defaultStarTheme(): StarGraphTheme {
  return { version: 1, mode: "group", unified: "#446f9a", groups: { source: "#6c8199", wiki: "#456bb0", term: "#218a81", template: "#a47948" },
    edge: "#a1b5ce", highlight: "#bc633b", nodes: {}, labels: "auto", physics: { ...STAR_DEFAULT_PHYSICS } };
}
export function starColor(value: unknown): string | null {
  if (typeof value !== "string") return null;
  if (/^#[a-f0-9]{6}$/i.test(value)) return value.toLowerCase();
  if (/^#[a-f0-9]{3}$/i.test(value)) return "#" + [...value.slice(1)].map((character) => character.repeat(2)).join("").toLowerCase();
  return null;
}
function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
export function starNodeKey(value: string): boolean {
  return /^[a-z0-9][a-z0-9:_-]{0,127}$/i.test(value) && !["constructor", "prototype", "__proto__"].includes(value);
}
/** Whitelist only UI colors, bounded physical values, and opaque IDs. Never persist node text. */
export function normalizeStarTheme(value: unknown): StarGraphTheme {
  const source = record(value);
  const result = defaultStarTheme();
  if (source.mode === "group" || source.mode === "unified") result.mode = source.mode;
  for (const key of ["unified", "edge", "highlight"] as const) result[key] = starColor(source[key]) || result[key];
  const groups = record(source.groups);
  for (const key of STAR_GROUPS) result.groups[key] = starColor(groups[key]) || result.groups[key];
  result.nodes = Object.create(null) as Record<string, string>;
  for (const [id, color] of Object.entries(record(source.nodes)).slice(-200)) {
    const valid = starColor(color);
    if (starNodeKey(id) && valid) result.nodes[id] = valid;
  }
  if (source.labels === "auto" || source.labels === "all" || source.labels === "none") result.labels = source.labels;
  result.physics = normalizeStarPhysics(source.physics);
  return result;
}
export function nodeStarColor(theme: StarGraphTheme, id: string, group: StarGroup, accent = false): string {
  return theme.nodes[id] || (accent ? theme.highlight : theme.mode === "unified" ? theme.unified : theme.groups[group]);
}
export function starPreferenceStorageKey(preferenceKey?: string): string | null {
  if (!preferenceKey || preferenceKey.length > 160 || /[\u0000-\u001f]/.test(preferenceKey)) return null;
  return `fkb:ui:star-graph:v1:${encodeURIComponent(preferenceKey)}`;
}
export function parseStarTheme(raw: string | null): StarGraphTheme {
  if (!raw || raw.length > 40000) return defaultStarTheme();
  try { return normalizeStarTheme(JSON.parse(raw)); } catch { return defaultStarTheme(); }
}
export function readStarTheme(key: string | null, storage?: Pick<Storage, "getItem">): StarGraphTheme {
  if (!key) return defaultStarTheme();
  try { return parseStarTheme((storage || window.localStorage).getItem(key)); } catch { return defaultStarTheme(); }
}
export function saveStarTheme(key: string | null, value: StarGraphTheme, storage?: Pick<Storage, "setItem">): boolean {
  if (!key) return false;
  try { (storage || window.localStorage).setItem(key, JSON.stringify(normalizeStarTheme(value))); return true; } catch { return false; }
}
const subscribers = new Map<string, Set<() => void>>();
const volatile = new Map<string, StarGraphTheme>();
function rememberVolatile(key: string, theme: StarGraphTheme) {
  if (volatile.size >= 8 && !volatile.has(key)) volatile.delete(volatile.keys().next().value!);
  volatile.set(key, theme);
}
function publish(key: string) { subscribers.get(key)?.forEach((notify) => notify()); }

export function useStarGraphTheme(preferenceKey?: string) {
  const key = starPreferenceStorageKey(preferenceKey);
  const initial = () => key && volatile.has(key) ? volatile.get(key)! : readStarTheme(key);
  const [state, setState] = useState(() => ({ key, theme: initial(), persistence: "initial" }));
  // Until the subscription effect catches up, never expose the previous space's preference.
  const theme = state.key === key ? state.theme : initial();
  useEffect(() => {
    const update = () => setState({ key, theme: key && volatile.has(key) ? volatile.get(key)! : readStarTheme(key), persistence: "initial" });
    update();
    if (!key || typeof window === "undefined") return;
    const listeners = subscribers.get(key) || new Set<() => void>();
    listeners.add(update); subscribers.set(key, listeners);
    const onStorage = (event: StorageEvent) => {
      if (event.key !== key) return;
      volatile.delete(key);
      setState({ key, theme: parseStarTheme(event.newValue), persistence: "initial" });
    };
    window.addEventListener("storage", onStorage);
    return () => {
      listeners.delete(update);
      if (!listeners.size) subscribers.delete(key);
      window.removeEventListener("storage", onStorage);
    };
  }, [key]);
  function change(next: StarGraphTheme) {
    const normalized = normalizeStarTheme(next);
    const saved = saveStarTheme(key, normalized);
    if (key) {
      if (saved) volatile.delete(key); else rememberVolatile(key, normalized);
      publish(key);
    }
    setState({ key, theme: normalized, persistence: saved ? "saved" : "session" });
  }
  function reset() {
    let saved = !key;
    if (key) {
      try { window.localStorage.removeItem(key); volatile.delete(key); saved = true; }
      catch { rememberVolatile(key, defaultStarTheme()); }
      publish(key);
    }
    setState({ key, theme: defaultStarTheme(), persistence: saved && key ? "saved" : "session" });
  }
  return { theme, change, reset, persistence: state.key === key ? state.persistence : "initial" };
}
