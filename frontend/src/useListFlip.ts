import { useRef, type RefObject } from "react";
import gsap from "gsap";
import { Flip } from "gsap/Flip";
import { useGSAP } from "@gsap/react";
import { useMotionPreferences } from "./motionPreferences";

gsap.registerPlugin(useGSAP, Flip);

const MAX_ITEMS = 40;
const STATIC_CONTENT = '.ProseMirror, .tiptap, [contenteditable]:not([contenteditable="false"]), textarea, '
  + 'input:not([type="checkbox"]):not([type="radio"]):not([type="button"]):not([type="submit"]), '
  + 'select, [data-motion-static], [data-kb-motion="content"], .document-paper, .wiki-reading-body, .segment-editor';
type SavedItem = { element: HTMLElement; state: Flip.FlipState };

const eligible = (element: HTMLElement) => Boolean(element.dataset.flipId) && element.isConnected
  && !element.closest(STATIC_CONTENT) && !element.querySelector(STATIC_CONTENT)
  && !element.closest('[hidden], [aria-hidden="true"], [inert]');

/** Native intersection reporting includes nested overflow clips. Registering
 * candidates reads no layout; scrolling never queries/measures the whole list. */
function createVisibility(root: HTMLElement, changed: () => void) {
  const Observer = root.ownerDocument.defaultView?.IntersectionObserver;
  if (!Observer) return null; // Static fallback; never guess that the first 40 are visible.
  let disposed = false;
  const observed = new Map<HTMLElement, number>();
  const visible = new Set<HTMLElement>();
  const receive = (entries: IntersectionObserverEntry[]) => {
    if (disposed) return;
    for (const entry of entries) {
      const node = entry.target as HTMLElement;
      if (!observed.has(node) || !root.contains(node)) continue;
      if (entry.isIntersecting && entry.intersectionRect.width > 0 && entry.intersectionRect.height > 0) visible.add(node);
      else visible.delete(node);
    }
  };
  const observer = new Observer(entries => { receive(entries); if (!disposed) changed(); },
    { root: null, rootMargin: "0px", threshold: 0 });
  return {
    root,
    sync(selector: string) {
      // Once per identity/DOM change only, and no getBoundingClientRect/style reads.
      const candidates = Array.from(root.querySelectorAll<HTMLElement>(selector)).filter(eligible);
      const counts = new Map<string, number>();
      for (const node of candidates) counts.set(node.dataset.flipId!, (counts.get(node.dataset.flipId!) || 0) + 1);
      const next = new Set(candidates.filter(node => counts.get(node.dataset.flipId!) === 1));
      for (const node of observed.keys()) if (!next.has(node)) {
        observer.unobserve(node); observed.delete(node); visible.delete(node);
      }
      let order = 0;
      for (const node of next) {
        const known = observed.has(node);
        observed.set(node, order++);
        if (!known) observer.observe(node);
      }
      receive(observer.takeRecords());
    },
    items(): HTMLElement[] {
      if (disposed || root.ownerDocument.visibilityState === "hidden") return [];
      return Array.from(visible).filter(node => root.contains(node) && eligible(node) && !Flip.isFlipping(node))
        .sort((a, b) => observed.get(a)! - observed.get(b)!).slice(0, MAX_ITEMS);
    },
    dispose() {
      disposed = true; observer.disconnect(); observed.clear(); visible.clear();
    },
  };
}

/**
 * Capture the last committed layout, then Flip surviving visible items after
 * React commits a new identity. The caller owns DOM order and stable data-flip-id.
 * Removed elements are never cloned, animated out, reinserted, or retained.
 */
export function useListFlip<T extends HTMLElement>(
  rootRef: RefObject<T | null>, identity: unknown, selector = "[data-flip-id]",
): void {
  const { effective, reduced } = useMotionPreferences();
  const saved = useRef(new Map<string, SavedItem>());
  const active = useRef<gsap.core.Timeline | null>(null);
  const generation = useRef(0);
  const refreshLayout = useRef<() => void>(() => {});
  const scheduleCapture = useRef<() => void>(() => {});
  const visibilityTracker = useRef<ReturnType<typeof createVisibility>>(null);

  useGSAP(() => {
    const root = rootRef.current;
    if (!root) return;
    const view = root.ownerDocument.defaultView;
    if (!view) return;
    let frame = 0;
    let disposed = false;
    const refresh = () => {
      if (disposed || frame || active.current?.isActive()) return;
      frame = view.requestAnimationFrame(() => {
        frame = 0;
        if (!disposed && !active.current?.isActive()) refreshLayout.current();
      });
    };
    scheduleCapture.current = refresh;
    const visibility = () => {
      if (root.ownerDocument.visibilityState === "hidden") active.current?.progress(1);
      else refresh();
    };
    const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(refresh);
    resize?.observe(root);
    root.addEventListener("scroll", refresh, { passive: true, capture: true });
    view.addEventListener("resize", refresh, { passive: true });
    view.addEventListener("scroll", refresh, { passive: true });
    root.ownerDocument.addEventListener("visibilitychange", visibility);
    return () => {
      disposed = true;
      generation.current++;
      if (frame) view.cancelAnimationFrame(frame);
      resize?.disconnect();
      visibilityTracker.current?.dispose();
      visibilityTracker.current = null;
      scheduleCapture.current = () => {};
      root.removeEventListener("scroll", refresh, true);
      view.removeEventListener("resize", refresh);
      view.removeEventListener("scroll", refresh);
      root.ownerDocument.removeEventListener("visibilitychange", visibility);
      active.current?.revert();
      active.current = null;
      for (const item of saved.current.values()) item.state.clear();
      saved.current.clear();
      refreshLayout.current = () => {};
    };
  }, { scope: rootRef, dependencies: [rootRef], revertOnUpdate: true });

  useGSAP((_context, contextSafe) => {
    const root = rootRef.current;
    const token = ++generation.current;
    let disposed = false;
    active.current = null;
    const release = () => {
      for (const item of saved.current.values()) item.state.clear();
      saved.current.clear();
    };
    if (!root || effective === "quiet" || reduced || root.ownerDocument.visibilityState === "hidden") {
      visibilityTracker.current?.dispose();
      visibilityTracker.current = null;
      release();
      refreshLayout.current = () => {};
      return;
    }
    if (visibilityTracker.current?.root !== root) {
      visibilityTracker.current?.dispose();
      visibilityTracker.current = createVisibility(root, () => scheduleCapture.current());
    }
    const tracker = visibilityTracker.current;
    if (!tracker) { release(); refreshLayout.current = () => {}; return; }
    tracker.sync(selector);
    const capture = () => {
      if (disposed || generation.current !== token || !root.isConnected) return;
      const next = new Map<string, SavedItem>();
      for (const element of tracker.items()) {
        next.set(element.dataset.flipId!, { element, state: Flip.getState(element, { simple: true }) });
      }
      release();
      saved.current = next;
    };
    refreshLayout.current = contextSafe!(capture);
    const current = tracker.items();
    const previous = Flip.getState([], { simple: true });
    const survivors: HTMLElement[] = [];
    for (const element of current) {
      const old = saved.current.get(element.dataset.flipId!);
      // A replaced node may have different/withdrawn content: animate only the
      // same live React element, not a stale DOM node with a recycled ID.
      if (old?.element === element) {
        previous.add(old.state);
        survivors.push(element);
      }
    }
    release();
    // Measure the new committed DOM BEFORE Flip writes any inverted transform.
    capture();
    if (survivors.length) {
      const finish = (completed: boolean) => {
        previous.clear();
        if (disposed || generation.current !== token) return;
        active.current = null;
        if (completed) refreshLayout.current();
      };
      active.current = Flip.from(previous, {
        targets: survivors, duration: effective === "rich" ? .42 : .24,
        ease: "power3.inOut", simple: true, scale: true, absolute: false,
        absoluteOnLeave: false, fade: false, prune: true,
        stagger: { amount: effective === "rich" ? .075 : .035 },
        onComplete: () => finish(true), onInterrupt: () => finish(false),
      });
    } else previous.clear();
    return () => {
      disposed = true;
      previous.clear();
      refreshLayout.current = () => {};
    };
  }, { scope: rootRef, dependencies: [rootRef, identity, selector, effective, reduced], revertOnUpdate: true });
}

export default useListFlip;
