import { useEffect, useRef, useState, type RefObject } from "react";
import { createPortal } from "react-dom";
import { Sparkle } from "@phosphor-icons/react";
import gsap from "gsap";
import { useGSAP } from "@gsap/react";
import { useMotionPreferences, type MotionLevel } from "./motionPreferences";
import { isGraphInteracting, subscribeGraphInteraction } from "./visualInteraction";

gsap.registerPlugin(useGSAP);
export type AmbientEffectsProps = {
  rootRef: RefObject<HTMLElement | null>;
  routeKey: string;
};
export const AMBIENT_LIMITS = Object.freeze({
  particles: 72,
  mobileParticles: 24,
  links: 110,
  trails: 8,
  fps: 30,
  surfaces: 12,
  titles: 2,
  pixels: 3_000_000,
});
type Quality = "rich" | "balanced" | "economy" | "quiet";
type Budget = {
  particles: number;
  links: number;
  trails: number;
  fps: number;
  surfaces: number;
  quality: Quality;
};
const desktopBudgets: readonly Budget[] = [
  {
    particles: 72,
    links: 110,
    trails: 8,
    fps: 30,
    surfaces: 12,
    quality: "rich",
  },
  {
    particles: 44,
    links: 66,
    trails: 5,
    fps: 24,
    surfaces: 8,
    quality: "balanced",
  },
  {
    particles: 26,
    links: 34,
    trails: 3,
    fps: 15,
    surfaces: 4,
    quality: "economy",
  },
];
const mobileBudgets: readonly Budget[] = [
  {
    particles: 24,
    links: 36,
    trails: 5,
    fps: 30,
    surfaces: 6,
    quality: "rich",
  },
  {
    particles: 18,
    links: 26,
    trails: 3,
    fps: 24,
    surfaces: 4,
    quality: "balanced",
  },
  {
    particles: 12,
    links: 14,
    trails: 0,
    fps: 15,
    surfaces: 2,
    quality: "economy",
  },
];
const quietBudget: Budget = {
  particles: 0,
  links: 0,
  trails: 0,
  fps: 0,
  surfaces: 0,
  quality: "quiet",
};

export class AmbientFrameBudget {
  private grade: number;
  private requestedGrade: number;
  private lastPaint: number | null = null;
  private badFrames = 0;
  private costAverage = 0;
  private gapAverage = 0;
  private samples = 0;
  lastGap = 0;
  constructor(
    public level: MotionLevel,
    public mobile: boolean,
  ) {
    this.grade = this.requestedGrade = level === "rich" ? 0 : 1;
  }
  get budget(): Budget {
    return this.level === "quiet"
      ? quietBudget
      : (this.mobile ? mobileBudgets : desktopBudgets)[this.grade];
  }
  get degraded() {
    return this.level !== "quiet" && this.grade > this.requestedGrade;
  }
  resetClock() {
    this.lastPaint = null;
    this.lastGap = 0;
    this.badFrames = 0;
    this.samples = 0;
    this.costAverage = 0;
    this.gapAverage = 0;
  }
  accept(now: number) {
    if (!Number.isFinite(now) || !this.budget.fps) return false;
    const interval = 1000 / this.budget.fps;
    if (this.lastPaint !== null && now - this.lastPaint + 0.05 < interval)
      return false;
    this.lastGap =
      this.lastPaint === null ? interval : Math.max(0, now - this.lastPaint);
    this.lastPaint = now;
    return true;
  }
  record(costMs: number) {
    if (this.level === "quiet" || this.grade >= 2 || !Number.isFinite(costMs))
      return false;
    const cost = Math.max(0, Math.min(1000, costMs));
    this.costAverage = this.samples
      ? this.costAverage * 0.82 + cost * 0.18
      : cost;
    this.gapAverage = this.samples
      ? this.gapAverage * 0.82 + this.lastGap * 0.18
      : this.lastGap;
    this.samples++;
    const slow =
      this.costAverage > (this.grade ? 13 : 10) ||
      this.gapAverage > (1000 / this.budget.fps) * 1.9;
    this.badFrames =
      slow && this.samples > 3
        ? this.badFrames + 1
        : Math.max(0, this.badFrames - 1);
    return this.badFrames >= 18 ? this.downgrade() : false;
  }
  longTask(duration: number) {
    if (this.level === "quiet" || this.grade >= 2 || duration < 80)
      return false;
    this.badFrames += 6;
    return this.badFrames >= 18 ? this.downgrade() : false;
  }
  private downgrade() {
    this.grade = Math.min(2, this.grade + 1);
    this.resetClock();
    return true;
  }
}
export function clampMagneticOffset(
  x: number,
  y: number,
  result = { x: 0, y: 0 },
) {
  if (!Number.isFinite(x) || !Number.isFinite(y)) {
    result.x = 0;
    result.y = 0;
    return result;
  }
  const distance = Math.hypot(x, y);
  const scale = distance > 4 ? 4 / distance : 1;
  result.x = x * scale;
  result.y = y * scale;
  return result;
}
function numericSetter(
  target: HTMLElement,
  property: string,
  unit?: string,
): (value: number) => void {
  const setter = gsap.quickSetter(target, property, unit);
  return (value) => {
    setter(value);
  };
}
export type AmbientStats = {
  running: boolean;
  reason: string;
  quality: Quality;
  degraded: boolean;
  particles: number;
  links: number;
  trailPoints: number;
  fpsCap: number;
  frames: number;
  observedFps: number;
  frameIntervalMs: number;
  averageDrawMs: number;
  longTasks: number;
  surfaces: number;
  visibleSurfaces: number;
};
type AmbientClock = {
  now: () => number;
  add: (callback: () => void) => void;
  remove: (callback: () => void) => void;
};
type Surface = {
  node: HTMLElement;
  edge: HTMLSpanElement;
  light: HTMLSpanElement;
  sheen: HTMLSpanElement;
  rotate: (value: number) => void;
  sx: (value: number) => void;
  sy: (value: number) => void;
  alpha: (value: number) => void;
  visible: boolean;
  phase: number;
  previous: string | null;
};
type Title = {
  node: HTMLElement;
  x: (value: number) => void;
  visible: boolean;
  phase: number;
  previous: string | null;
  previousPosition: string;
};
type Magnet = {
  button: HTMLButtonElement;
  icon: SVGElement;
  context: gsap.Context;
  toX: ReturnType<typeof gsap.quickTo>;
  toY: ReturnType<typeof gsap.quickTo>;
  rect: DOMRect;
  previousX: string;
  previousY: string;
  previousMarker: string | null;
};
const CARD_SELECTOR =
  ".models-provider-card, .model-connection-row, .models-connection-detail, .wiki-page-index li > button, .resource-overview";
const TITLE_SELECTOR = ".page-heading h1, [data-ambient-title]";
const PROTECTED =
  'form, input, textarea, select, [contenteditable], .ProseMirror, .tiptap, .document-workbench, .document-paper, .segment-handle, [draggable="true"], [data-drag-handle], [data-ambient-ignore], .ambient-controls, .wiki-graph, .star-graph, [data-graph-interactive], canvas, tr';
const colors = ["#3978cf", "#7964d6", "#299ead"] as const;

export function createAmbientRuntime(options: {
  root: HTMLElement;
  layer: HTMLElement;
  canvas: HTMLCanvasElement;
  auroras: HTMLElement[];
  trailNodes: HTMLElement[];
  level: MotionLevel;
  seed?: number;
  clock?: AmbientClock;
  onState?: (stats: AmbientStats) => void;
}) {
  const { root, layer, canvas, auroras, trailNodes, level } = options;
  const previousReady = layer.getAttribute("data-ambient-ready");
  layer.removeAttribute("data-ambient-ready");
  const doc = root.ownerDocument;
  const win = doc.defaultView!;
  const clock: AmbientClock = options.clock ?? {
    now: () => win.performance.now(),
    add: (callback) => gsap.ticker.add(callback),
    remove: (callback) => gsap.ticker.remove(callback),
  };
  const finePointer = win.matchMedia("(pointer: fine) and (hover: hover)");
  const coarsePointer = win.matchMedia("(pointer: coarse)");
  let mobile = win.innerWidth <= 820 || coarsePointer.matches;
  let governor = new AmbientFrameBudget(level, mobile);
  let disposed = false;
  let visible = true;
  let focused = doc.hasFocus();
  let attached = false;
  let pressed = false;
  let width = Math.max(1, win.innerWidth);
  let height = Math.max(1, win.innerHeight);
  let dpr = 1;
  let elapsed = 0;
  let activeAt: number | null = null;
  let lastReport = 0;
  let framesAtReport = 0;
  let registered = false;
  let layoutDirty = true;
  let resizeTimer: ReturnType<typeof setTimeout> | undefined;
  let mutationTimer: ReturnType<typeof setTimeout> | undefined;
  let visibilityTimer: ReturnType<typeof setTimeout> | undefined;
  let auroraTimeline: gsap.core.Timeline | undefined;
  let magnet: Magnet | undefined;
  let pointerCard: HTMLElement | null = null;
  let pointerButton: HTMLButtonElement | null = null;
  let activeCard: Surface | undefined;
  let cardBounds: DOMRect | undefined;
  let seed = options.seed ?? 613;
  const random = () => {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    return seed / 4294967296;
  };
  const points = new Float32Array(AMBIENT_LIMITS.particles * 6);
  const indices = new Uint8Array(AMBIENT_LIMITS.particles);
  const linked = new Uint8Array(
    AMBIENT_LIMITS.particles * AMBIENT_LIMITS.particles,
  );
  const tails = new Float32Array(AMBIENT_LIMITS.trails * 3);
  const pointer = {
    x: 0,
    y: 0,
    active: false,
    lastX: -1000,
    lastY: -1000,
    nextTail: 0,
  };
  const magneticOffset = { x: 0, y: 0 };
  const surfaces = new Map<HTMLElement, Surface>();
  const titles = new Map<HTMLElement, Title>();
  const originalRoot = new Map(
    [
      "data-ambient-shell",
      "data-ambient-level",
      "data-ambient-running",
      "data-ambient-quality",
    ].map((name) => [name, root.getAttribute(name)]),
  );
  const stats: AmbientStats = {
    running: false,
    reason: "initializing",
    quality: governor.budget.quality,
    degraded: false,
    particles: governor.budget.particles,
    links: 0,
    trailPoints: 0,
    fpsCap: governor.budget.fps,
    frames: 0,
    observedFps: 0,
    frameIntervalMs: 0,
    averageDrawMs: 0,
    longTasks: 0,
    surfaces: 0,
    visibleSurfaces: 0,
  };
  const context = gsap.context(() => {}, root);
  let graphics: CanvasRenderingContext2D | null = null;
  try {
    graphics = canvas.getContext("2d", { alpha: true });
  } catch {
    /* A blocked drawing surface is explicitly reported below. */
  }
  const trailSetters = trailNodes
    .slice(0, 8)
    .map((node) => ({
      x: gsap.quickSetter(node, "x", "px"),
      y: gsap.quickSetter(node, "y", "px"),
      alpha: gsap.quickSetter(node, "opacity"),
    }));
  let observer: IntersectionObserver | undefined;
  const restoreAttribute = (
    node: Element,
    name: string,
    previous: string | null,
  ) =>
    previous === null
      ? node.removeAttribute(name)
      : node.setAttribute(name, previous);

  function report(force = false) {
    const now = clock.now();
    if (!force && now - lastReport < 1000) return;
    if (!force && now > lastReport)
      stats.observedFps =
        ((stats.frames - framesAtReport) * 1000) / (now - lastReport);
    lastReport = now;
    framesAtReport = stats.frames;
    layer.dataset.ambientRunning = String(stats.running);
    layer.dataset.ambientQuality = stats.quality;
    layer.dataset.ambientReason = stats.reason;
    canvas.dataset.particleCount = String(stats.particles);
    canvas.dataset.lineCount = String(stats.links);
    canvas.dataset.trailCount = String(stats.trailPoints);
    canvas.dataset.fpsCap = String(stats.fpsCap);
    canvas.dataset.frameCount = String(stats.frames);
    canvas.dataset.frameIntervalMs = stats.frameIntervalMs.toFixed(2);
    canvas.dataset.observedFps = stats.observedFps.toFixed(1);
    canvas.dataset.averageDrawMs = stats.averageDrawMs.toFixed(2);
    canvas.dataset.longTasks = String(stats.longTasks);
    if (force) options.onState?.({ ...stats });
  }
  function seedPoints() {
    const capacity = mobile ? 24 : 72;
    const cols = Math.max(2, Math.ceil(Math.sqrt((capacity * width) / height)));
    const rows = Math.ceil(capacity / cols);
    for (let i = 0; i < capacity; i++) {
      const offset = i * 6;
      points[offset] = (((i % cols) + 0.28 + random() * 0.44) / cols) * width;
      points[offset + 1] =
        ((Math.floor(i / cols) + 0.28 + random() * 0.44) / rows) * height;
      points[offset + 2] = (random() < 0.5 ? -1 : 1) * (8 + random() * 10);
      points[offset + 3] = (random() < 0.5 ? -1 : 1) * (5 + random() * 7);
      points[offset + 4] = random() * Math.PI * 2;
      points[offset + 5] = 1.5 + random() * 1.45;
    }
    for (let i = 0; i < governor.budget.particles; i++)
      indices[i] = Math.floor((i * capacity) / governor.budget.particles);
  }
  function clearMagnet() {
    if (!magnet) return;
    magnet.toX.tween.kill();
    magnet.toY.tween.kill();
    magnet.context.revert();
    magnet.previousX
      ? magnet.icon.style.setProperty("--ambient-magnet-x", magnet.previousX)
      : magnet.icon.style.removeProperty("--ambient-magnet-x");
    magnet.previousY
      ? magnet.icon.style.setProperty("--ambient-magnet-y", magnet.previousY)
      : magnet.icon.style.removeProperty("--ambient-magnet-y");
    restoreAttribute(
      magnet.button,
      "data-ambient-magnetic",
      magnet.previousMarker,
    );
    magnet = undefined;
  }
  function clearInteraction() {
    pointer.active = false;
    pointerCard = null;
    pointerButton = null;
    tails.fill(0);
    stats.trailPoints = 0;
    clearMagnet();
    for (const setters of trailSetters) setters.alpha(0);
    if (activeCard) activeCard.alpha(0);
    activeCard = undefined;
    cardBounds = undefined;
  }
  function dropSurface(record: Surface) {
    if (
      activeCard === record ||
      record.node.contains(pointerCard) ||
      record.node.contains(pointerButton) ||
      (magnet && record.node.contains(magnet.button))
    )
      clearInteraction();
    observer?.unobserve(record.node);
    record.edge.remove();
    record.sheen.remove();
    restoreAttribute(record.node, "data-ambient-card", record.previous);
    surfaces.delete(record.node);
  }
  function clearDisconnectedInteraction() {
    // Check only the current targets, before any layout reads. A removed target
    // need not emit pointerleave, and can also be moved outside this root.
    if (
      (pointerCard && (!pointerCard.isConnected || !root.contains(pointerCard))) ||
      (pointerButton && (!pointerButton.isConnected || !root.contains(pointerButton))) ||
      (activeCard && (!activeCard.node.isConnected || !root.contains(activeCard.node))) ||
      (magnet && (!magnet.button.isConnected || !root.contains(magnet.button) || !magnet.icon.isConnected))
    )
      clearInteraction();
  }
  function dropTitle(record: Title) {
    observer?.unobserve(record.node);
    restoreAttribute(record.node, "data-ambient-title-active", record.previous);
    record.previousPosition ? record.node.style.setProperty('--ambient-title-position', record.previousPosition) : record.node.style.removeProperty('--ambient-title-position');
    titles.delete(record.node);
  }
  function discover() {
    if (disposed) return;
    for (const record of surfaces.values())
      if (!root.contains(record.node) || !record.edge.isConnected)
        dropSurface(record);
    clearDisconnectedInteraction();
    for (const record of titles.values())
      if (!root.contains(record.node) || !record.node.isConnected)
        dropTitle(record);
    const candidates: { node: HTMLElement; rect: DOMRect; title: boolean }[] =
      [];
    if (governor.budget.surfaces) {
      for (const node of root.querySelectorAll<HTMLElement>(CARD_SELECTOR)) {
        if (
          surfaces.size +
            candidates.filter((candidate) => !candidate.title).length >=
          governor.budget.surfaces
        )
          break;
        if (!surfaces.has(node) && !node.closest(PROTECTED))
          candidates.push({
            node,
            rect: node.getBoundingClientRect(),
            title: false,
          });
      }
      for (const node of root.querySelectorAll<HTMLElement>(TITLE_SELECTOR)) {
        if (
          titles.size +
            candidates.filter((candidate) => candidate.title).length >=
          2
        )
          break;
        if (!titles.has(node) && !node.closest(PROTECTED) && (node.textContent?.trim().length ?? 0) > 0 && (node.textContent?.trim().length ?? 0) <= 64)
          candidates.push({
            node,
            rect: node.getBoundingClientRect(),
            title: true,
          });
      }
    }
    // Layout reads above; additions/style writes below. No per-frame discovery.
    for (const candidate of candidates) {
      const inView =
        candidate.rect.width > 0 &&
        candidate.rect.height > 0 &&
        candidate.rect.top < height &&
        candidate.rect.bottom > 0;
      if (candidate.title) {
        // Paint a bounded gradient inside the original glyphs. No duplicated
        // heading text or moving rectangle; removal restores the existing styles.
        const previous = candidate.node.getAttribute('data-ambient-title-active');
        const previousPosition = candidate.node.style.getPropertyValue('--ambient-title-position');
        candidate.node.setAttribute('data-ambient-title-active', '');
        candidate.node.style.setProperty('--ambient-title-position', '0%');
        const record: Title = {
          node: candidate.node,
          x: numericSetter(candidate.node, '--ambient-title-position', '%'),
          visible: inView,
          phase: titles.size * 1.8,
          previous,
          previousPosition,
        };
        titles.set(record.node, record);
        observer?.observe(record.node);
      } else {
        const edge = doc.createElement("span");
        edge.className = "ambient-card-edge";
        edge.setAttribute("aria-hidden", "true");
        edge.inert = true;
        const light = doc.createElement("span");
        light.className = "ambient-card-edge-light";
        edge.append(light);
        const sheen = doc.createElement("span");
        sheen.className = "ambient-card-sheen";
        sheen.setAttribute("aria-hidden", "true");
        sheen.inert = true;
        sheen.style.setProperty("--ambient-spot-x", "0px");
        sheen.style.setProperty("--ambient-spot-y", "0px");
        sheen.style.setProperty("--ambient-spot-opacity", "0");
        const previous = candidate.node.getAttribute("data-ambient-card");
        candidate.node.setAttribute("data-ambient-card", "");
        candidate.node.append(edge, sheen);
        const record: Surface = {
          node: candidate.node,
          edge,
          light,
          sheen,
          rotate: numericSetter(light, "rotation", "deg"),
          sx: numericSetter(sheen, "--ambient-spot-x", "px"),
          sy: numericSetter(sheen, "--ambient-spot-y", "px"),
          alpha: numericSetter(sheen, "--ambient-spot-opacity"),
          visible: inView,
          phase: surfaces.size * 31,
          previous,
        };
        surfaces.set(record.node, record);
        observer?.observe(record.node);
      }
    }
    stats.surfaces = surfaces.size;
  }
  function applyBudget() {
    const budget = governor.budget;
    stats.quality = budget.quality;
    stats.degraded = governor.degraded;
    stats.particles = budget.particles;
    stats.fpsCap = budget.fps;
    root.dataset.ambientQuality = budget.quality;
    const capacity = mobile ? 24 : 72;
    for (let i = 0; i < budget.particles; i++)
      indices[i] = Math.floor((i * capacity) / budget.particles);
    let retained = 0;
    for (const record of surfaces.values())
      if (++retained > budget.surfaces) dropSurface(record);
    if (!budget.surfaces)
      for (const record of titles.values()) dropTitle(record);
    clearInteraction();
    discover();
    report(true);
  }
  function resize() {
    if (disposed) return;
    const nextMobile = win.innerWidth <= 820 || coarsePointer.matches;
    const nextWidth = Math.max(1, win.innerWidth);
    const nextHeight = Math.max(1, win.innerHeight);
    const nextDpr = Math.min(
      win.devicePixelRatio || 1,
      1.5,
      Math.sqrt(AMBIENT_LIMITS.pixels / (nextWidth * nextHeight)),
    );
    const changed =
      width !== nextWidth ||
      height !== nextHeight ||
      dpr !== nextDpr ||
      mobile !== nextMobile ||
      !registered;
    if (!changed) {
      layoutDirty = true;
      return;
    }
    width = nextWidth;
    height = nextHeight;
    dpr = Math.max(0.1, nextDpr);
    if (mobile !== nextMobile) {
      mobile = nextMobile;
      governor = new AmbientFrameBudget(level, mobile);
    }
    canvas.width = level === "quiet" ? 1 : Math.max(1, Math.floor(width * dpr));
    canvas.height =
      level === "quiet" ? 1 : Math.max(1, Math.floor(height * dpr));
    graphics?.setTransform(dpr, 0, 0, dpr, 0, 0);
    seedPoints();
    layoutDirty = true;
    registered = true;
    applyBudget();
    if (!stats.running) draw(0);
  }
  function resizeSoon() {
    if (disposed) return;
    layoutDirty = true;
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(resize, 80);
  }
  function pointerMove(event: PointerEvent) {
    if (
      !stats.running ||
      !finePointer.matches ||
      event.pointerType === "touch" ||
      event.pointerType === "pen"
    )
      return;
    const target = event.target instanceof Element ? event.target : null;
    if (event.buttons || pressed || !target || target.closest(PROTECTED)) {
      clearInteraction();
      return;
    }
    pointer.x = event.clientX;
    pointer.y = event.clientY;
    pointer.active = true;
    const card = target.closest<HTMLElement>(CARD_SELECTOR);
    pointerCard = card && root.contains(card) ? card : null;
    const button = target.closest<HTMLButtonElement>("button.primary");
    pointerButton =
      button &&
      root.contains(button) &&
      !button.disabled &&
      !button.matches('.danger,[aria-disabled="true"],[data-danger]') &&
      !button.closest(PROTECTED)
        ? button
        : null;
  }
  function press() {
    pressed = true;
    clearInteraction();
  }
  function release() {
    pressed = false;
  }
  function leave() {
    clearInteraction();
  }
  function focusInput(event: FocusEvent) {
    if (event.target instanceof Element && event.target.closest(PROTECTED))
      clearInteraction();
  }

  function updatePointerEffects(dt: number) {
    clearDisconnectedInteraction();
    // At most two reads, on target changes/scroll/resize. All writes follow them.
    const nextCard = pointerCard ? surfaces.get(pointerCard) : undefined;
    const nextCardBounds =
      nextCard && (nextCard !== activeCard || layoutDirty)
        ? nextCard.node.getBoundingClientRect()
        : cardBounds;
    const nextButtonBounds =
      pointerButton &&
      (!magnet || magnet.button !== pointerButton || layoutDirty)
        ? pointerButton.getBoundingClientRect()
        : magnet?.rect;
    layoutDirty = false;
    if (activeCard !== nextCard) {
      activeCard?.alpha(0);
      activeCard = nextCard;
    }
    cardBounds = nextCardBounds;
    if (activeCard && cardBounds && pointer.active && !pressed) {
      activeCard.sx(pointer.x - cardBounds.left);
      activeCard.sy(pointer.y - cardBounds.top);
      activeCard.alpha(1);
    }
    if (
      !pointerButton ||
      pointerButton.disabled ||
      !pointerButton.isConnected ||
      !pointer.active
    )
      clearMagnet();
    else if (
      !magnet ||
      magnet.button !== pointerButton ||
      !magnet.icon.isConnected
    ) {
      clearMagnet();
      const icon = pointerButton.querySelector<SVGElement>(
        ":scope > svg:not(.spin)",
      );
      if (icon && nextButtonBounds) {
        const previousX = icon.style.getPropertyValue("--ambient-magnet-x");
        const previousY = icon.style.getPropertyValue("--ambient-magnet-y");
        const previousMarker = pointerButton.getAttribute(
          "data-ambient-magnetic",
        );
        icon.style.setProperty("--ambient-magnet-x", "0px");
        icon.style.setProperty("--ambient-magnet-y", "0px");
        pointerButton.setAttribute("data-ambient-magnetic", "");
        // The icon follows; the real button hit area, text, and form controls never move.
        // The short-lived hover context is disposed on leave. Do not append every
        // hover's killed tweens to the route-long context (which would retain them).
        let toX!: ReturnType<typeof gsap.quickTo>;
        let toY!: ReturnType<typeof gsap.quickTo>;
        const hoverContext = gsap.context(() => {
          toX = gsap.quickTo(icon, "--ambient-magnet-x", {
            duration: 0.16,
            ease: "power2.out",
          });
          toY = gsap.quickTo(icon, "--ambient-magnet-y", {
            duration: 0.16,
            ease: "power2.out",
          });
        }, icon);
        magnet = {
          button: pointerButton,
          icon,
          context: hoverContext,
          rect: nextButtonBounds,
          previousX,
          previousY,
          previousMarker,
          toX,
          toY,
        };
      }
    }
    if (magnet && nextButtonBounds) {
      magnet.rect = nextButtonBounds;
      const mx =
        ((pointer.x - magnet.rect.left) / Math.max(1, magnet.rect.width) -
          0.5) *
        8;
      const my =
        ((pointer.y - magnet.rect.top) / Math.max(1, magnet.rect.height) -
          0.5) *
        8;
      const offset = clampMagneticOffset(mx, my, magneticOffset);
      magnet.toX(offset.x);
      magnet.toY(offset.y);
    }
    const limit = finePointer.matches ? governor.budget.trails : 0;
    if (
      pointer.active &&
      limit &&
      Math.hypot(pointer.x - pointer.lastX, pointer.y - pointer.lastY) > 4
    ) {
      const index = (pointer.nextTail++ % limit) * 3;
      tails[index] = pointer.x;
      tails[index + 1] = pointer.y;
      tails[index + 2] = 1;
      pointer.lastX = pointer.x;
      pointer.lastY = pointer.y;
    }
    stats.trailPoints = 0;
    for (let i = 0; i < 8; i++) {
      const offset = i * 3;
      tails[offset + 2] =
        i >= limit ? 0 : Math.max(0, tails[offset + 2] - dt / 0.26);
      const life = tails[offset + 2];
      const setters = trailSetters[i];
      if (!setters) continue;
      if (life > 0) {
        stats.trailPoints++;
        setters.x(tails[offset]);
        setters.y(tails[offset + 1]);
        setters.alpha(life * 0.72);
      } else setters.alpha(0);
    }
  }

  function draw(dt: number) {
    if (!graphics) return;
    const ctx = graphics;
    const budget = governor.budget;
    const count = budget.particles;
    ctx.clearRect(0, 0, width, height);
    stats.links = 0;
    if (!count) return;
    for (let n = 0; n < count; n++) {
      const i = indices[n] * 6;
      points[i] += points[i + 2] * dt;
      points[i + 1] += points[i + 3] * dt;
      if (points[i] < 8 || points[i] > width - 8) points[i + 2] *= -1;
      if (points[i + 1] < 8 || points[i + 1] > height - 8) points[i + 3] *= -1;
      points[i] = Math.min(width - 4, Math.max(4, points[i]));
      points[i + 1] = Math.min(height - 4, Math.max(4, points[i + 1]));
    }
    linked.fill(0);
    const radius = Math.min(
      245,
      Math.max(125, Math.sqrt((width * height) / count) * 1.58),
    );
    const radiusSquared = radius * radius;
    // Two bounded nearest-neighbour passes distribute lines across the whole page.
    for (let pass = 0; pass < 2 && stats.links < budget.links; pass++) {
      for (let a = 0; a < count && stats.links < budget.links; a++) {
        const ai = indices[a];
        const ax = points[ai * 6];
        const ay = points[ai * 6 + 1];
        let nearest = -1;
        let best = radiusSquared;
        for (let b = 0; b < count; b++) {
          const bi = indices[b];
          if (a === b || linked[ai * 72 + bi]) continue;
          const dx = ax - points[bi * 6];
          const dy = ay - points[bi * 6 + 1];
          const distance = dx * dx + dy * dy;
          if (distance < best) {
            best = distance;
            nearest = bi;
          }
        }
        if (nearest < 0) continue;
        linked[ai * 72 + nearest] = 1;
        linked[nearest * 72 + ai] = 1;
        ctx.globalAlpha = 0.12 + 0.25 * (1 - Math.sqrt(best) / radius);
        ctx.strokeStyle = colors[a % 3];
        ctx.lineWidth = 0.8;
        ctx.beginPath();
        ctx.moveTo(ax, ay);
        ctx.lineTo(points[nearest * 6], points[nearest * 6 + 1]);
        ctx.stroke();
        stats.links++;
      }
    }
    for (let n = 0; n < count; n++) {
      const i = indices[n] * 6;
      const phase = Math.sin(elapsed * 1.4 + points[i + 4]);
      const radius = points[i + 5] + phase * 0.4;
      ctx.fillStyle = colors[n % 3];
      ctx.globalAlpha = 0.09;
      ctx.beginPath();
      ctx.arc(points[i], points[i + 1], radius * 2.6, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 0.54 + phase * 0.2;
      ctx.beginPath();
      ctx.arc(points[i], points[i + 1], radius, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }
  function tick() {
    if (!stats.running || disposed) return;
    const now = clock.now();
    if (!governor.accept(now)) return;
    const dt = Math.min(
      0.08,
      activeAt === null
        ? 1 / governor.budget.fps
        : Math.max(0, (now - activeAt) / 1000),
    );
    activeAt = now;
    elapsed += dt;
    // Read pointer rectangles before compositor/canvas writes in this frame.
    updatePointerEffects(dt);
    auroraTimeline?.totalTime(elapsed, true);
    stats.visibleSurfaces = 0;
    for (const surface of surfaces.values())
      if (surface.visible) {
        surface.rotate(elapsed * 35 + surface.phase);
        stats.visibleSurfaces++;
      }
    for (const title of titles.values())
      if (title.visible) {
        const phase = ((elapsed + title.phase) % 4.8) / 4.8;
        // With no-repeat and transparent text fill, leaving 0..100% can move
        // the entire gradient away from the glyphs and make the heading vanish.
        title.x((1 - Math.cos(phase * Math.PI * 2)) * 50);
      }
    draw(dt);
    const cost = Math.max(0, clock.now() - now);
    stats.frames++;
    stats.frameIntervalMs = governor.lastGap;
    stats.averageDrawMs =
      stats.frames === 1 ? cost : stats.averageDrawMs * 0.85 + cost * 0.15;
    if (governor.record(cost)) applyBudget();
    report();
  }
  function activity() {
    if (disposed) return;
    const reason =
      level === "quiet"
        ? "quiet"
        : !graphics
          ? "canvas-unavailable"
          : doc.visibilityState === "hidden"
            ? "hidden"
            : !focused
              ? "blurred"
              : !visible
                ? "offscreen"
                : isGraphInteracting()
                  ? "graph-interaction"
                  : "running";
    const running = reason === "running";
    if (running === stats.running && stats.reason === reason) return;
    stats.running = running;
    stats.reason = reason;
    root.dataset.ambientRunning = String(running);
    if (running) {
      governor.resetClock();
      activeAt = null;
      lastReport = clock.now();
      framesAtReport = stats.frames;
      if (!attached) {
        clock.add(tick);
        attached = true;
      }
    } else {
      if (attached) {
        clock.remove(tick);
        attached = false;
      }
      pressed = false;
      clearInteraction();
      governor.resetClock();
      activeAt = null;
      stats.observedFps = 0;
    }
    report(true);
  }
  const onVisibility = () => activity();
  const onBlur = () => {
    focused = false;
    activity();
  };
  const onFocus = () => {
    focused = true;
    activity();
  };
  const onScroll = () => {
    layoutDirty = true;
    if (!observer && !visibilityTimer)
      visibilityTimer = setTimeout(() => {
        visibilityTimer = undefined;
        if (disposed) return;
        const rect = root.getBoundingClientRect();
        visible = rect.bottom > 0 && rect.top < height && rect.width > 0;
        activity();
      }, 100);
  };
  const onPointerDevice = () => {
    clearInteraction();
    resize();
    activity();
  };
  root.setAttribute("data-ambient-shell", "");
  root.dataset.ambientLevel = level;
  root.dataset.ambientRunning = "false";
  if (typeof win.IntersectionObserver === "function") {
    observer = new win.IntersectionObserver((entries) => {
      if (disposed) return;
      for (const entry of entries) {
        if (entry.target === root) visible = entry.isIntersecting;
        else {
          const surface = surfaces.get(entry.target as HTMLElement);
          if (surface) surface.visible = entry.isIntersecting;
          const title = titles.get(entry.target as HTMLElement);
          if (title) title.visible = entry.isIntersecting;
        }
      }
      activity();
    });
    observer.observe(root);
  } else {
    const rect = root.getBoundingClientRect();
    visible = rect.bottom > 0 && rect.top < height;
  }
  let resizeObserver: ResizeObserver | undefined;
  if (typeof win.ResizeObserver === "function") {
    resizeObserver = new win.ResizeObserver(resizeSoon);
    resizeObserver.observe(root);
  }
  const mutationObserver = new MutationObserver((records) => {
    if (disposed) return;
    if (
      !records.some((record) => {
        if (
          record.target instanceof Element &&
          record.target.closest(PROTECTED)
        )
          return false;
        return [...record.addedNodes, ...record.removedNodes].some(
          (node) =>
            node instanceof HTMLElement &&
            !node.className.toString().startsWith("ambient-"),
        );
      })
    )
      return;
    if (mutationTimer) clearTimeout(mutationTimer);
    mutationTimer = setTimeout(() => {
      if (disposed) return;
      discover();
      layoutDirty = true;
    }, 80);
  });
  mutationObserver.observe(root, { childList: true, subtree: true });
  let longTaskObserver: PerformanceObserver | undefined;
  if (typeof win.PerformanceObserver === "function") {
    try {
      longTaskObserver = new win.PerformanceObserver((list) => {
        if (disposed || !stats.running) return;
        for (const entry of list.getEntries()) {
          stats.longTasks++;
          if (governor.longTask(entry.duration)) applyBudget();
        }
      });
      longTaskObserver.observe({ type: "longtask", buffered: false });
    } catch {
      longTaskObserver?.disconnect();
      longTaskObserver = undefined;
    }
  }
  context.add(() => {
    if (level !== "quiet") {
      auroraTimeline = gsap.timeline({
        paused: true,
        repeat: -1,
        yoyo: true,
        defaults: { ease: "sine.inOut" },
      });
      auroraTimeline.addLabel("field", 0);
      if (auroras[0])
        auroraTimeline.fromTo(
          auroras[0],
          { xPercent: -12, yPercent: -7, rotation: -10, scale: 0.96 },
          {
            xPercent: 12,
            yPercent: 11,
            rotation: 9,
            scale: 1.13,
            duration: 14,
          },
          "field",
        );
      if (auroras[1])
        auroraTimeline.fromTo(
          auroras[1],
          { xPercent: 11, yPercent: 9, rotation: 7, scale: 1.05 },
          {
            xPercent: -13,
            yPercent: -9,
            rotation: -8,
            scale: 0.92,
            duration: 18,
          },
          "field",
        );
      if (auroras[2])
        auroraTimeline.fromTo(
          auroras[2],
          { xPercent: -8, yPercent: 8, scale: 0.93 },
          { xPercent: 13, yPercent: -12, scale: 1.15, duration: 16 },
          "field",
        );
    }
  });
  root.addEventListener("pointermove", pointerMove, { passive: true });
  root.addEventListener("pointerleave", leave, { passive: true });
  root.addEventListener("pointerdown", press, { passive: true });
  root.addEventListener("dragstart", press, { passive: true });
  root.addEventListener("focusin", focusInput);
  win.addEventListener("pointerup", release, { passive: true });
  win.addEventListener("dragend", release, { passive: true });
  win.addEventListener("pointercancel", release, { passive: true });
  win.addEventListener("blur", onBlur);
  win.addEventListener("focus", onFocus);
  win.addEventListener("resize", resizeSoon, { passive: true });
  root.addEventListener("scroll", onScroll, { capture: true, passive: true });
  win.addEventListener("scroll", onScroll, { passive: true });
  doc.addEventListener("visibilitychange", onVisibility);
  finePointer.addEventListener("change", onPointerDevice);
  coarsePointer.addEventListener("change", onPointerDevice);
  // Aggregate transitions only, never one notification per input/frame. activity()
  // reads the current snapshot, including a gesture that began before this mount.
  const unsubscribeGraphInteraction = subscribeGraphInteraction(activity);
  resize();
  discover();
  activity();
  report(true);
  layer.setAttribute("data-ambient-ready", "true");
  return {
    getStats: () => ({ ...stats }),
    dispose() {
      if (disposed) return;
      disposed = true;
      unsubscribeGraphInteraction();
      if (attached) clock.remove(tick);
      attached = false;
      if (resizeTimer) clearTimeout(resizeTimer);
      if (mutationTimer) clearTimeout(mutationTimer);
      if (visibilityTimer) clearTimeout(visibilityTimer);
      observer?.disconnect();
      resizeObserver?.disconnect();
      mutationObserver.disconnect();
      longTaskObserver?.disconnect();
      root.removeEventListener("pointermove", pointerMove);
      root.removeEventListener("pointerleave", leave);
      root.removeEventListener("pointerdown", press);
      root.removeEventListener("dragstart", press);
      root.removeEventListener("focusin", focusInput);
      win.removeEventListener("pointerup", release);
      win.removeEventListener("dragend", release);
      win.removeEventListener("blur", onBlur);
      win.removeEventListener("focus", onFocus);
      win.removeEventListener("resize", resizeSoon);
      win.removeEventListener("pointercancel", release);
      root.removeEventListener("scroll", onScroll, true);
      doc.removeEventListener("visibilitychange", onVisibility);
      win.removeEventListener("scroll", onScroll);
      finePointer.removeEventListener("change", onPointerDevice);
      coarsePointer.removeEventListener("change", onPointerDevice);
      clearInteraction();
      auroraTimeline?.kill();
      context.revert();
      for (const record of surfaces.values()) dropSurface(record);
      for (const record of titles.values()) dropTitle(record);
      for (const [name, value] of originalRoot)
        restoreAttribute(root, name, value);
      restoreAttribute(layer, "data-ambient-ready", previousReady);
      graphics?.clearRect(0, 0, width, height);
      for (const node of trailNodes) {
        node.style.removeProperty("transform");
        node.style.removeProperty("opacity");
      }
      stats.running = false;
      stats.reason = "disposed";
      stats.surfaces = 0;
      stats.visibleSurfaces = 0;
    },
  };
}

export function AmbientEffects({ rootRef, routeKey }: AmbientEffectsProps) {
  const preferences = useMotionPreferences();
  const layerRef = useRef<HTMLDivElement>(null);
  const trailRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [rootElement, setRootElement] = useState<HTMLElement | null>(null);
  const [panelHost, setPanelHost] = useState<HTMLElement | null>(null);
  const [runtimeState, setRuntimeState] = useState<{
    degraded: boolean;
    reason: string;
  }>({ degraded: false, reason: "initializing" });
  // Parent object refs may still be null during a child's initial layout effect.
  // Capture the committed root in a passive effect, then initialize from state.
  useEffect(() => {
    const committedRoot = rootRef.current;
    setRootElement(committedRoot);
    setPanelHost(
      committedRoot?.querySelector<HTMLElement>(".topbar-right") ?? null,
    );
  }, [rootRef, routeKey]);
  useGSAP(
    () => {
      const root = rootElement;
      const layer = layerRef.current;
      const canvas = canvasRef.current;
      if (!root || !root.isConnected || !layer || !canvas) return;
      const controller = createAmbientRuntime({
        root,
        layer,
        canvas,
        level: preferences.effective,
        seed: Array.from(routeKey).reduce(
          (sum, char) => (sum * 31 + char.charCodeAt(0)) >>> 0,
          613,
        ),
        auroras: Array.from(
          layer.querySelectorAll<HTMLElement>(".ambient-aurora"),
        ),
        trailNodes: Array.from(
          trailRef.current?.children ?? [],
        ) as HTMLElement[],
        onState: (state) =>
          setRuntimeState((previous) =>
            previous.degraded === state.degraded &&
            previous.reason === state.reason
              ? previous
              : { degraded: state.degraded, reason: state.reason },
          ),
      });
      return () => controller.dispose();
    },
    {
      scope: layerRef,
      dependencies: [rootElement, preferences.effective, routeKey],
      revertOnUpdate: true,
    },
  );
  const hint = preferences.reduced
    ? "系统减少动效已启用，所有循环已暂停"
    : runtimeState.degraded
      ? "已根据实测负载自动降低装饰密度与帧率"
      : preferences.effective === "quiet"
        ? "静谧模式，所有装饰循环已暂停"
        : "星光仅作界面装饰，不是知识图谱节点";
  const panel = (
    <div
      className={
        "ambient-controls " + (!panelHost ? "ambient-controls-fallback" : "")
      }
      role="group"
      aria-label="界面动效档位"
      title={hint}
    >
      <Sparkle size={17} aria-hidden="true" />
      <label>
        <span className="ambient-controls-label">动效</span>
        <select
          aria-label="界面动效档位"
          value={preferences.level}
          onChange={(event) =>
            preferences.setLevel(event.target.value as MotionLevel)
          }
        >
          <option value="rich">丰富</option>
          <option value="balanced">均衡</option>
          <option value="quiet">静谧</option>
        </select>
      </label>
      {(preferences.reduced || runtimeState.degraded) && (
        <small role="status">
          {preferences.reduced ? "系统静谧" : "自动节能"}
        </small>
      )}
    </div>
  );
  return (
    <>
      <div
        ref={layerRef}
        className="ambient-effects"
        data-ambient-level={preferences.effective}
        aria-hidden="true"
      >
        <div className="ambient-aurora ambient-aurora-one" />
        <div className="ambient-aurora ambient-aurora-two" />
        <div className="ambient-aurora ambient-aurora-three" />
        <canvas ref={canvasRef} className="ambient-starfield" />
      </div>
      <div ref={trailRef} className="ambient-trail-layer" aria-hidden="true">
        {Array.from({ length: 8 }, (_, i) => (
          <span className="ambient-trail-dot" key={i} />
        ))}
      </div>
      {panelHost ? createPortal(panel, panelHost) : panel}
    </>
  );
}
export default AmbientEffects;
