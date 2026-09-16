import { useRef, type ReactNode } from "react";
import gsap from "gsap";
import { useGSAP } from "@gsap/react";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import { useMotionPreferences } from "./motionPreferences";

gsap.registerPlugin(useGSAP, ScrollTrigger);
export const motionTiming = Object.freeze({
  page: 0.36,
  item: 0.28,
  overview: 0.34,
  modalIn: 0.36,
  modalOut: 0.3,
  feedback: 0.32,
  exit: 0.28,
});
export const prefersReducedMotion = () =>
  typeof window === "undefined" ||
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;
type MotionPreset = "page" | "overview" | "selection" | "content";
const animatedProperties = "opacity,visibility,transform";
const protectedContent = '.ProseMirror, .tiptap, [contenteditable]:not([contenteditable="false"]), '
  + 'textarea, select, input:not([type="checkbox"]):not([type="radio"]):not([type="button"]):not([type="submit"]), '
  + '[data-motion-static], .document-editor, .document-reader, .document-paper, .wiki-reading-body, .segment-editor';
const revealSelector = '.resource-table tbody > tr:nth-child(-n+6), .plain-table tbody > tr:nth-child(-n+6), '
  + '.models-provider-card, .models-connections-list > button, .wiki-page-index > ul > li, [data-motion-reveal]';
const canAnimate = (element: HTMLElement) => !element.closest(protectedContent)
  && !element.querySelector(protectedContent)
  && !element.closest('[hidden], [aria-hidden="true"], [inert]');
const inViewport = (element: HTMLElement) => {
  const rect = element.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0 && rect.top < window.innerHeight
    && rect.bottom > 0 && rect.left < window.innerWidth && rect.right > 0;
};

function scrollContainer(element: HTMLElement, root: HTMLElement): HTMLElement | undefined {
  let parent = element.parentElement;
  while (parent) {
    const style = getComputedStyle(parent);
    if (/(auto|scroll)/.test(style.overflowY) && parent.scrollHeight > parent.clientHeight) return parent;
    if (parent === root) break;
    parent = parent.parentElement;
  }
  return undefined;
}

function snapshotExit(element: HTMLElement) {
  const rect = element.getBoundingClientRect();
  if (
    !rect.width ||
    !rect.height ||
    prefersReducedMotion() ||
    element.ownerDocument.visibilityState === "hidden"
  )
    return;
  const copy = element.cloneNode(true) as HTMLElement;
  copy.classList.add("kb-motion-exit");
  copy.setAttribute("aria-hidden", "true");
  copy.removeAttribute("role");
  copy.removeAttribute("aria-live");
  copy.inert = true;
  for (const node of [
    copy,
    ...copy.querySelectorAll<HTMLElement>(
      "[id], [role], [aria-live], input, button, select, textarea, a",
    ),
  ]) {
    node.removeAttribute("id");
    node.removeAttribute("role");
    node.removeAttribute("aria-live");
    node.setAttribute("tabindex", "-1");
  }
  Object.assign(copy.style, {
    position: "fixed",
    left: rect.left + "px",
    top: rect.top + "px",
    right: "auto",
    bottom: "auto",
    width: rect.width + "px",
    height: rect.height + "px",
    maxWidth: "none",
    margin: "0",
    transform: "none",
    translate: "none",
    pointerEvents: "none",
    zIndex: "151",
  });
  return copy;
}

// React has already removed these conditional feedback elements. This bounded,
// inert visual copy cannot execute their former buttons or announce stale status.
function runExit(copy: HTMLElement | undefined, afterClean?: () => void) {
  if (
    !copy ||
    prefersReducedMotion() ||
    copy.ownerDocument.visibilityState === "hidden"
  )
    return;
  const doc = copy.ownerDocument;
  const media = window.matchMedia("(prefers-reduced-motion: reduce)");
  let cleaned = false;
  let context: gsap.Context | undefined;
  let timeout: ReturnType<typeof setTimeout> | undefined;
  const clean = () => {
    if (cleaned) return;
    cleaned = true;
    if (timeout) clearTimeout(timeout);
    media.removeEventListener("change", clean);
    window.removeEventListener("pagehide", clean);
    context?.revert();
    copy.remove();
    afterClean?.();
  };
  doc.body.appendChild(copy);
  context = gsap.context(() => {
    gsap.to(copy, {
      autoAlpha: 0,
      y: 10,
      scale: 0.985,
      duration: motionTiming.exit,
      ease: "power2.in",
      onComplete: clean,
    });
  }, copy);
  timeout = setTimeout(clean, 600);
  media.addEventListener("change", clean);
  window.addEventListener("pagehide", clean, { once: true });
  return clean;
}

function toastFeedback(scope: HTMLElement) {
  // The known React root is discovered from this page; other applications and
  // document-wide selectors are never scanned or observed.
  const appRoot = scope.closest<HTMLElement>("#root");
  if (!appRoot) return () => {};
  const live = new Map<
    HTMLElement,
    {
      mutation: MutationObserver;
      context: gsap.Context;
      snapshot?: HTMLElement;
    }
  >();
  const exits = new Set<() => void>();
  const attach = (node: HTMLElement) => {
    if (!node.matches(".toast") || live.has(node)) return;
    const snapshot = snapshotExit(node);
    const context = gsap.context(() => {
      if (prefersReducedMotion()) return;
      // GSAP preserves the existing CSS translateX(-50%) centering.
      gsap.fromTo(
        node,
        { autoAlpha: 0.15, y: 14 },
        {
          autoAlpha: 1,
          y: 0,
          duration: motionTiming.feedback,
          ease: "power2.out",
          clearProps: animatedProperties,
        },
      );
    }, node);
    const updateSnapshot = () => {
      const record = live.get(node);
      if (record) record.snapshot = snapshotExit(node);
    };
    const mutation = new MutationObserver(() => {
      updateSnapshot();
      if (prefersReducedMotion()) return;
      context.add(() => {
        gsap.fromTo(
          node,
          { opacity: 0.6 },
          {
            opacity: 1,
            duration: motionTiming.item,
            ease: "power1.out",
            overwrite: "auto",
            clearProps: "opacity",
          },
        );
      });
    });
    mutation.observe(node, {
      childList: true,
      characterData: true,
      subtree: true,
    });
    live.set(node, { mutation, context, snapshot });
  };
  for (const child of appRoot.children)
    if (child instanceof HTMLElement) attach(child);
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      for (const node of record.removedNodes) {
        if (!(node instanceof HTMLElement)) continue;
        const item = live.get(node);
        if (!item) continue;
        item.mutation.disconnect();
        item.context.revert();
        live.delete(node);
        const clean = runExit(item.snapshot, () => {
          if (clean) exits.delete(clean);
        });
        if (clean) exits.add(clean);
      }
      for (const node of record.addedNodes)
        if (node instanceof HTMLElement) attach(node);
    }
  });
  observer.observe(appRoot, { childList: true });
  const media = window.matchMedia("(prefers-reduced-motion: reduce)");
  const reduce = () => {
    if (media.matches) {
      for (const { context } of live.values()) context.revert();
      for (const clean of exits) clean();
      exits.clear();
    }
  };
  media.addEventListener("change", reduce);
  return () => {
    observer.disconnect();
    media.removeEventListener("change", reduce);
    for (const entry of live.values()) {
      entry.mutation.disconnect();
      entry.context.revert();
    }
    live.clear();
    for (const clean of exits) clean();
    exits.clear();
  };
}

export type MotionProps = {
  children: ReactNode;
  className?: string;
  identity?: unknown;
  preset?: MotionPreset;
  stagger?: boolean;
};
export function Motion({
  children,
  className = "",
  identity,
  preset,
  stagger = true,
}: MotionProps) {
  const ref = useRef<HTMLDivElement>(null);
  const { effective, reduced } = useMotionPreferences();
  const kind =
    preset ??
    (className.split(/\s+/).includes("page-root")
      ? "page"
      : className.split(/\s+/).includes("resource-overview")
        ? "overview"
        : className.split(/\s+/).includes("selection-bar")
          ? "selection"
          : "content");
  useGSAP(
    (_context, contextSafe) => {
      const root = ref.current;
      if (!root) return;
      if (effective === "quiet" || reduced || root.ownerDocument.visibilityState === "hidden") return;
      const seen = new WeakSet<Element>();
      const triggers = new Map<HTMLElement, ScrollTrigger>();
      const animations = new Set<gsap.core.Animation>();
      const rich = effective === "rich";
      let disposed = false;
      const timeline = gsap.timeline({ data: `kb-motion:${kind}`, defaults: {
        ease: "power3.out", duration: rich ? motionTiming.item : .2,
        clearProps: animatedProperties,
      } });
      animations.add(timeline);
      timeline.addLabel("surface", 0).addLabel("heading", .025).addLabel("content", .055);
      if (canAnimate(root)) {
        timeline.fromTo(root, {
          autoAlpha: .2, y: kind === "overview" ? 0 : kind === "selection" ? 18 : rich ? 14 : 6,
          ...(kind === "overview" ? { x: rich ? 18 : 8 } : {}),
          ...(kind === "selection" ? { scale: .97 } : {}),
        }, { autoAlpha: 1, x: 0, y: 0, scale: 1,
          duration: rich ? kind === "overview" ? motionTiming.overview
            : kind === "page" ? motionTiming.page : motionTiming.feedback : .22 }, "surface");
      }
      if (kind === "overview") {
        const sections = Array.from(root.children).filter((node): node is HTMLElement =>
          node instanceof HTMLElement && canAnimate(node)).slice(0, rich ? 8 : 4);
        if (sections.length) timeline.fromTo(sections, { x: rich ? 14 : 6, autoAlpha: .25 }, {
          x: 0, autoAlpha: 1, stagger: { amount: rich ? .12 : .06 },
        }, "heading");
      }
      const enter = contextSafe!((nodes: HTMLElement[], initial = false) => {
        if (disposed || root.ownerDocument.visibilityState === "hidden") return;
        const fresh = nodes.filter(node => root.contains(node) && !seen.has(node) && canAnimate(node));
        fresh.forEach(node => seen.add(node));
        if (!fresh.length) return;
        const destination = { y: 0, autoAlpha: 1, duration: rich ? motionTiming.item : .18,
          stagger: { amount: rich ? .12 : .045 }, ease: "power2.out", clearProps: animatedProperties };
        if (initial) timeline.fromTo(fresh, { y: rich ? 10 : 4, autoAlpha: .25 }, destination, "content");
        else {
          const tween = gsap.fromTo(fresh, { y: rich ? 10 : 4, autoAlpha: .25 }, destination);
          animations.add(tween);
          tween.eventCallback("onComplete", () => animations.delete(tween));
        }
      });
      const observe = contextSafe!((candidates: HTMLElement[], initial = false) => {
        const ready: HTMLElement[] = [];
        for (const node of candidates) {
          if (seen.has(node) || triggers.has(node) || !canAnimate(node) || !root.contains(node)) continue;
          const scroller = scrollContainer(node, root);
          const bounds = node.getBoundingClientRect();
          const clip = scroller?.getBoundingClientRect();
          if (inViewport(node) && (!clip || bounds.bottom > clip.top && bounds.top < clip.bottom)) ready.push(node);
          else if (triggers.size < (rich ? 24 : 12)) {
            // Do not hide off-screen content upfront. Trigger only this surface,
            // never pin/scrub prose or install a global scroll cleanup.
            const trigger = ScrollTrigger.create({ trigger: node, scroller,
              start: "top 94%", once: true, onEnter: () => enter([node]),
              onEnterBack: () => enter([node]) });
            triggers.set(node, trigger);
          }
        }
        enter(ready.slice(0, rich ? 12 : 6), initial);
      });
      let observer: MutationObserver | undefined;
      let refreshFrame = 0;
      if (kind === "page" && stagger) {
        const headings = Array.from(root.querySelectorAll<HTMLElement>(
          ":scope > .page-heading > div, :scope > .tabs")).filter(canAnimate).slice(0, 3);
        if (headings.length) timeline.fromTo(headings, { y: rich ? 9 : 4, autoAlpha: .3 }, {
          y: 0, autoAlpha: 1, stagger: rich ? .035 : .02,
        }, "heading");
        observe(Array.from(root.querySelectorAll<HTMLElement>(revealSelector)).slice(0, 30), true);
        observer = new MutationObserver(records => {
          const added: HTMLElement[] = [];
          for (const record of records) {
            if (record.target instanceof HTMLElement && record.target.closest(protectedContent)) continue;
            for (const node of record.addedNodes) {
              if (!(node instanceof HTMLElement) || node.closest(protectedContent) || added.length >= 30) continue;
              if (node.matches(revealSelector)) added.push(node);
              else if (node.matches("table, tbody, .table-scroll, .resource-workspace, .resource-main, .standard-page, "
                + ".models-provider-grid, .models-connections-list, .wiki-page-index, .wiki-pages-layout, .wiki-workbench")) {
                added.push(...Array.from(node.querySelectorAll<HTMLElement>(revealSelector)).slice(0, 30 - added.length));
              }
            }
          }
          for (const [node, trigger] of triggers) if (!root.contains(node)) { trigger.kill(); triggers.delete(node); }
          if (added.length) observe(added);
          if (triggers.size && !refreshFrame) refreshFrame = window.requestAnimationFrame(() => {
            refreshFrame = 0;
            if (!disposed) for (const trigger of triggers.values()) trigger.refresh();
          });
        });
        observer.observe(root, { childList: true, subtree: true });
      }
      const finishVisible = () => { for (const animation of animations) animation.progress(1); };
      const focus = (event: FocusEvent) => {
        if (event.target instanceof HTMLElement && event.target.closest(protectedContent)) finishVisible();
      };
      const visibility = () => { if (root.ownerDocument.visibilityState === "hidden") finishVisible(); };
      root.addEventListener("focusin", focus);
      root.ownerDocument.addEventListener("visibilitychange", visibility);
      return () => {
        disposed = true;
        observer?.disconnect();
        if (refreshFrame) window.cancelAnimationFrame(refreshFrame);
        for (const trigger of triggers.values()) trigger.kill();
        triggers.clear();
        animations.clear();
        root.removeEventListener("focusin", focus);
        root.ownerDocument.removeEventListener("visibilitychange", visibility);
      };
    },
    {
      scope: ref,
      dependencies: [identity, kind, stagger, effective, reduced],
      revertOnUpdate: true,
    },
  );

  useGSAP(
    () => {
      const root = ref.current;
      if (!root) return;
      if (effective === "quiet" || reduced) return;
      if (kind === "page") return toastFeedback(root);
      if (kind !== "selection") return;
      return () => {
        const copy = snapshotExit(root);
        queueMicrotask(() => {
          if (!root.isConnected) runExit(copy);
        });
      };
    },
    { scope: ref, dependencies: [kind, effective, reduced], revertOnUpdate: true },
  );
  return (
    <div ref={ref} className={className} data-kb-motion={kind} data-kb-motion-level={effective}>
      {children}
    </div>
  );
}

/** Lightweight paragraph/insert entrance. Identity changes animate once; typing
 * does not restart it. Keep identity equal to a stable block id. */
export function MotionItem({
  children,
  identity,
  className = "",
  active = false,
}: {
  children: ReactNode;
  identity?: unknown;
  className?: string;
  active?: boolean;
}) {
  return active ? (
    <Motion
      identity={identity}
      className={className}
      preset="content"
      stagger={false}
    >
      {children}
    </Motion>
  ) : (
    <div className={className}>{children}</div>
  );
}
