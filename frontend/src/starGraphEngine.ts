import gsap from "gsap";
import { StarGraphEngine, clampStar, STAR_WIDTH, STAR_HEIGHT, type StarCamera, type StarBounds } from "./starGraphPhysics";
export { StarGraphEngine, prepareStarGraph, clampStar, starRadius, STAR_WIDTH, STAR_HEIGHT, STAR_MAX_TICKS, STAR_STATIC_TICKS } from "./starGraphPhysics";
export type { StarNode, StarLink, StarCamera, StarBounds } from "./starGraphPhysics";
import type { WikiGraphData, WikiNode, GraphRenderBudget } from "./knowledgeTypes";
import { nodeStarColor, type StarGraphTheme, type StarPhysics } from "./starGraphTheme";
import { setGraphInteraction } from "./visualInteraction";

// Kept only for the SVG compatibility path. The production Canvas/WebGL runtime
// has one unthrottled display clock and an independent fixed-step worker.
export const STAR_MAX_FPS = 30;

export function fitStarCamera(bounds: StarBounds, width = STAR_WIDTH, height = STAR_HEIGHT, screenScale = 1): StarCamera {
  const scale = clampStar(screenScale, 0.1, 4);
  const labelReserve = Math.min(170, width * scale * 0.3) + 16;
  const k = clampStar(Math.min((width - 2 * labelReserve / scale) / Math.max(180, bounds.right - bounds.left),
    (height - 76 / scale) / Math.max(140, bounds.bottom - bounds.top)), 0.18, 1.5);
  return { k, x: width / 2 - (bounds.left + bounds.right) * k / 2, y: height / 2 - (bounds.top + bounds.bottom) * k / 2 };
}
export function starLabelLayout(label: string, nodeX: number, radius: number, camera: StarCamera, screenScale: number) {
  const scale = clampStar(screenScale, 0.1, 4);
  const factor = Math.max(0.018, camera.k * scale);
  const screenX = (camera.x + nodeX * camera.k) * scale;
  const gap = radius * factor + 7;
  const left = screenX - gap - 14;
  const right = STAR_WIDTH * scale - screenX - gap - 14;
  const onLeft = left > right;
  const budget = Math.max(0, Math.min(174, STAR_WIDTH * scale * 0.38, onLeft ? left : right));
  // Conservatively budget one em for every code point (including Latin), keeping
  // actual text inside the viewport without per-frame DOM text measurements.
  const count = Math.max(1, Math.floor(budget / 12.6));
  const characters = Array.from(label);
  return { text: characters.length > count ? characters.slice(0, Math.max(1, count - 1)).join("") + "…" : label,
    x: (onLeft ? -1 : 1) * (radius + 7 / factor), anchor: onLeft ? "end" : "start",
    fontSize: 12 / factor, budget };
}
export function zoomStarCamera(camera: StarCamera, ratio: number, point = { x: STAR_WIDTH / 2, y: STAR_HEIGHT / 2 }): StarCamera {
  const k = clampStar(camera.k * ratio, 0.18, 4);
  const scale = k / camera.k;
  return { k, x: point.x - (point.x - camera.x) * scale, y: point.y - (point.y - camera.y) * scale };
}
export function normalizeStarWheel(delta: number, mode: number, pageHeight: number): number {
  if (!Number.isFinite(delta)) return 0;
  const pixels = delta * (mode === 1 ? 16 : mode === 2 ? pageHeight : 1);
  return clampStar(pixels, -240, 240);
}
export function starWorldPoint(point: { x: number; y: number }, camera: StarCamera) {
  return { x: (point.x - camera.x) / camera.k, y: (point.y - camera.y) / camera.k };
}
export function starClientPoint(x: number, y: number, rect: { left: number; top: number; width: number; height: number }) {
  const scale = Math.min(rect.width / STAR_WIDTH, rect.height / STAR_HEIGHT);
  if (!Number.isFinite(scale) || scale <= 0) return null;
  return { x: (x - rect.left - (rect.width - STAR_WIDTH * scale) / 2) / scale,
    y: (y - rect.top - (rect.height - STAR_HEIGHT * scale) / 2) / scale };
}
export type StarActivity = { paused: boolean; visible: boolean; focused: boolean; onscreen: boolean; quiet: boolean };
export function starCanAnimate(activity: StarActivity): boolean {
  return !activity.paused && activity.visible && activity.focused && activity.onscreen && !activity.quiet;
}
/** Throttle this component only: never change global GSAP ticker.fps/lagSmoothing. */
export function createStarFrameGate() {
  let last = -Infinity;
  return (milliseconds: number, fps = STAR_MAX_FPS) => {
    const interval = 1000 / clampStar(fps, 1, STAR_MAX_FPS);
    if (milliseconds - last < interval - 0.01) return false;
    last = milliseconds; return true;
  };
}

export type StarPhase = "settling" | "settled" | "paused" | "quiet";
export type StarNodeElements = { position: SVGGElement; glyph: SVGGElement; dot: SVGCircleElement; text: SVGTextElement; halo?: SVGCircleElement };
export type StarRuntime = {
  zoom: (factor: number) => void; fit: () => void; replay: () => void; dispose: () => void;
  activity: (paused: boolean, quiet: boolean, level: string) => void;
  theme: (value: StarGraphTheme) => void; select: (id: string | null) => void;
};
type MountOptions = {
  surface: SVGSVGElement; viewport: SVGGElement; starLayer: SVGGElement; edgeLayer: SVGGElement;
  nodes: Map<string, StarNodeElements>; edges: Map<string, SVGPathElement>; graph: WikiGraphData; focusId?: string; renderBudget?: GraphRenderBudget;
  theme: StarGraphTheme; paused: boolean; quiet: boolean; level: string; selection: string | null;
  contextSafe: <T extends (...args: any[]) => any>(callback: T) => T;
  onOpen: (node: WikiNode) => void; onSelect: (id: string | null) => void; onPhase: (phase: StarPhase) => void;
};

/** Called inside useGSAP. All DOM references are captured once, never queried on animation frames. */
export function mountStarGraph(options: MountOptions): StarRuntime {
  const { surface, viewport: view } = options;
  const engine = new StarGraphEngine(options.graph, options.theme.physics, options.focusId, options.renderBudget);
  let theme = options.theme;
  let level = options.level;
  const activity: StarActivity = { paused: options.paused, quiet: options.quiet, visible: document.visibilityState !== "hidden",
    focused: document.hasFocus(), onscreen: typeof IntersectionObserver === "undefined" };
  let screenScale = 1;
  let camera = fitStarCamera(engine.bounds());
  let targetCamera = { ...camera };
  let cameraPending = false;
  let positionsPending = false;
  let selectionPending = false;
  let navigationActive = false;
  let priorityActive = false;
  let inputIdle = true;
  let interactionFrame = 0;
  let interactionTime = 0;
  let lastDragPhysicsTime = -Infinity;
  let interactionTimer: ReturnType<typeof setTimeout> | undefined;
  let surfaceRect: DOMRect | undefined;
  let reportedSelection = options.selection;
  const interactionOwner = {};
  let hovered: string | null = null;
  let hoverResumePoint: { x: number; y: number } | null = null;
  let selection = options.selection;
  let cameraTouched = false;
  let destroyed = false;
  let clockAttached = false;
  let frames = 0;
  let quietSettled = false;
  let currentPhase: StarPhase | null = null;
  let feedback: gsap.core.Tween | undefined;
  let suppressClick = false;
  let drag: { pointer: number; id: string | null; x: number; y: number; lastX: number; lastY: number; moved: boolean;
    rect: DOMRect; camera: StarCamera; offset: { x: number; y: number } } | null = null;
  const gate = createStarFrameGate();
  const ranked = new Set([...engine.nodes].sort((a, b) => b.degree - a.degree).slice(0, 30).map((node) => node.id));
  const nodeEntries = engine.nodes.map((node) => ({ node, elements: options.nodes.get(node.id)!, lastX: NaN, lastY: NaN })).filter((entry) => entry.elements);
  const edgeEntries = engine.links.map((edge) => ({ edge, element: options.edges.get(edge.id)! })).filter((entry) => entry.element);
  const physicsSignature = (value: StarGraphTheme) => [value.physics.nodeSize, value.physics.center, value.physics.repel, value.physics.distance, value.physics.linkStrength, value.physics.damping].join(":");
  let physicsKey = physicsSignature(theme);
  let paintCache = new WeakMap<Element, Map<string, string>>();
  let selectionSignature = "";
  const dirtyNodes = new Set<string>();
  function changed(element: Element, key: string, value: string) {
    let values = paintCache.get(element);
    if (!values) { values = new Map(); paintCache.set(element, values); }
    if (values.get(key) === value) return false;
    values.set(key, value); return true;
  }
  function attr(element: Element, name: string, value: string) {
    if (changed(element, name, value)) element.setAttribute(name, value);
  }
  function style(element: SVGElement, name: "opacity" | "display", value: string) {
    if (changed(element, `style:${name}`, value)) element.style[name] = value;
  }
  const entrance = gsap.timeline({ paused: true, defaults: { ease: "power2.out" } })
    .addLabel("stars", 0)
    .fromTo(options.starLayer, { opacity: 0.35 }, { opacity: 1, duration: 0.5, immediateRender: false }, "stars")
    .fromTo(options.edgeLayer, { opacity: 0 }, { opacity: 1, duration: 0.65, immediateRender: false }, "stars+=0.08");
  const status = (next: StarPhase) => {
    if (currentPhase === next || destroyed) return;
    currentPhase = next; surface.dataset.starPhase = next; options.onPhase(next);
  };
  const labelCache = new Map<string, string>();
  const paintLabels = () => {
    for (const { node, elements } of nodeEntries) {
      if (elements.text.style.display === "none") continue;
      const layout = starLabelLayout(node.item.label, node.x, node.radius, camera, screenScale);
      const signature = `${layout.text}:${layout.anchor}:${layout.x.toFixed(3)}:${layout.fontSize.toFixed(3)}`;
      if (labelCache.get(node.id) === signature) continue;
      labelCache.set(node.id, signature);
      elements.text.textContent = layout.text;
      elements.text.setAttribute("text-anchor", layout.anchor);
      elements.text.setAttribute("x", String(layout.x));
      elements.text.style.fontSize = `${layout.fontSize}px`;
    }
  };
  const renderCamera = (labels = true) => {
    attr(view, "transform", `translate(${camera.x.toFixed(2)} ${camera.y.toFixed(2)}) scale(${camera.k.toFixed(4)})`);
    if (labels && !navigationActive) paintLabels();
  };
  const paintPositions = (labels = true) => {
    dirtyNodes.clear();
    for (const entry of nodeEntries) {
      const { node, elements } = entry;
      if (entry.lastX === node.x && entry.lastY === node.y) continue;
      entry.lastX = node.x; entry.lastY = node.y; dirtyNodes.add(node.id);
      attr(elements.position, "transform", `translate(${node.x.toFixed(2)} ${node.y.toFixed(2)})`);
    }
    for (const { edge, element } of edgeEntries) if (dirtyNodes.has(edge.source.id) || dirtyNodes.has(edge.target.id))
      attr(element, "d", `M${edge.source.x.toFixed(2)},${edge.source.y.toFixed(2)}L${edge.target.x.toFixed(2)},${edge.target.y.toFixed(2)}`);
    attr(surface, "data-star-ticks", String(engine.totalTicks));
    if (labels && !navigationActive) paintLabels();
  };
  const paintSelection = () => {
    const active = hovered || (engine.byId.has(selection || "") ? selection : null);
    const signature = `${active}:${selection}:${camera.k >= 1.7}:${screenScale >= 0.55}`;
    if (selectionSignature === signature) return;
    selectionSignature = signature;
    const neighbors = active ? engine.adjacent.get(active) : null;
    for (const { node, elements } of nodeEntries) {
      style(elements.position, "opacity", !active || node.id === active || neighbors?.has(node.id) ? "1" : "0.23");
      if (changed(elements.position, "selected", String(node.id === selection))) elements.position.classList.toggle("is-selected", node.id === selection);
      attr(elements.dot, "r", String(node.radius));
      const color = nodeStarColor(theme, node.id, node.group, node.id === active || (node.id === engine.centerId && theme.mode === "group"));
      attr(elements.dot, "fill", color);
      if (elements.halo) {
        attr(elements.halo, "fill", color);
        attr(elements.halo, "r", String(node.radius * 2.7));
        if (!starCanAnimate(activity)) elements.halo.style.opacity = "0.1";
      }
      const show = theme.labels === "all" || (theme.labels === "auto" && (
        node.id === active || (screenScale >= 0.55 && Boolean(neighbors?.has(node.id)))
        || (engine.nodes.length <= 40 && node.degree >= 2)
        || (camera.k >= 1.7 && ranked.has(node.id))));
      style(elements.text, "display", show ? "" : "none");
    }
    for (const { edge, element } of edgeEntries) {
      const connected = active && (edge.source.id === active || edge.target.id === active);
      style(element, "opacity", active ? connected ? "0.94" : "0.09" : "0.58");
      attr(element, "stroke", connected ? theme.highlight : theme.edge);
      attr(element, "stroke-width", String(theme.physics.edgeWidth * (connected ? 1.5 : 1)));
    }
    paintLabels();
  };
  const paintPulse = (seconds: number) => {
    for (const { node, elements } of nodeEntries) {
      if (elements.halo) {
        // Static SVG geometry: animating r invalidates the whole SVG layout.
        // Only the bounded halo subset breathes; other solid dots stay unchanged.
        elements.dot.style.opacity = String(0.92 + 0.08 * Math.sin(seconds * 1.15 + node.phase));
        const pulse = (1 + Math.sin(seconds * 0.95 + node.phase)) / 2;
        elements.halo.style.opacity = String(0.05 + pulse * 0.13);
      }
    }
  };
  const stablePulse = () => { for (const { node, elements } of nodeEntries) {
    elements.dot.style.opacity = "1";
    if (elements.halo) { elements.halo.setAttribute("r", String(node.radius * 2.7)); elements.halo.style.opacity = "0.1"; }
  } };
  const fit = () => { camera = fitStarCamera(engine.bounds(), STAR_WIDTH, STAR_HEIGHT, screenScale); targetCamera = { ...camera }; renderCamera(); paintSelection(); };
  const measureViewport = () => {
    if (destroyed) return;
    const rect = surface.getBoundingClientRect();
    surfaceRect = rect;
    const next = Math.min(rect.width / STAR_WIDTH, rect.height / STAR_HEIGHT);
    if (Number.isFinite(next) && next > 0) screenScale = next;
    if (!cameraTouched) fit(); else paintSelection();
  };
  const frame = (seconds: number) => {
    if (destroyed || !starCanAnimate(activity) || priorityActive) return;
    const fps = engine.hot ? level === "rich" ? 30 : 24
      : engine.nodes.length > 100 ? level === "rich" ? 8 : 4 : level === "rich" ? 12 : 6;
    if (!gate(seconds * 1000, fps)) return;
    if (engine.hot) {
      engine.step(); paintPositions();
      if (!engine.hot) { if (!cameraTouched && !drag) fit(); status("settled"); }
    }
    paintPulse(seconds); frames++; surface.dataset.starFrames = String(frames);
  };
  const detachClock = () => {
    if (clockAttached) { gsap.ticker.remove(frame); clockAttached = false; }
    surface.dataset.starClock = "stopped";
  };
  const synchronize = () => {
    if (destroyed) return;
    if (!activity.visible || !activity.focused || !activity.onscreen || activity.paused) {
      detachClock(); entrance.progress(1).pause(); feedback?.pause(); status("paused"); return;
    }
    if (activity.quiet) {
      detachClock(); entrance.pause(); feedback?.pause();
      if (!quietSettled) { engine.settle(); quietSettled = true; paintPositions(); if (!cameraTouched) fit(); }
      entrance.progress(1).pause(); stablePulse(); status("quiet"); return;
    }
    if (!clockAttached) { gsap.ticker.add(frame); clockAttached = true; }
    surface.dataset.starClock = "running"; entrance.play(); feedback?.play(); status(engine.hot ? "settling" : "settled");
  };
  const canUseInteraction = () => !destroyed && activity.visible && activity.focused && activity.onscreen;
  function releasePriority() {
    if (priorityActive) { priorityActive = false; setGraphInteraction(interactionOwner, false); }
  }
  function finishInteraction() {
    if (interactionTimer !== undefined) clearTimeout(interactionTimer);
    interactionTimer = undefined; inputIdle = true; interactionTime = 0;
    navigationActive = false;
    surface.classList.remove("is-navigating");
    attr(surface, "data-star-interacting", "false");
    releasePriority();
    if (destroyed) return;
    selectionPending = false;
    paintSelection(); paintLabels(); synchronize();
  }
  function cancelInteraction(commit = false) {
    if (interactionFrame) cancelAnimationFrame(interactionFrame);
    interactionFrame = 0;
    if (!destroyed && commit) {
      if (cameraPending) { camera = { ...targetCamera }; renderCamera(false); }
      if (positionsPending) paintPositions(false);
    }
    targetCamera = { ...camera }; cameraPending = false; positionsPending = false;
    finishInteraction();
  }
  function requestInteractionFrame() {
    if (!interactionFrame && canUseInteraction()) interactionFrame = requestAnimationFrame(paintInteractionFrame);
  }
  function markInteraction(navigation: boolean) {
    if (!priorityActive) { priorityActive = true; setGraphInteraction(interactionOwner, true); }
    if (navigation && !navigationActive) {
      navigationActive = true; surface.classList.add("is-navigating");
    }
    attr(surface, "data-star-interacting", "true");
    inputIdle = false;
    if (interactionTimer !== undefined) clearTimeout(interactionTimer);
    interactionTimer = setTimeout(() => {
      interactionTimer = undefined; inputIdle = true;
      if (!destroyed && !drag && !cameraPending && !positionsPending) finishInteraction();
      else requestInteractionFrame();
    }, 120);
  }
  function paintInteractionFrame(time: number) {
    interactionFrame = 0;
    if (!canUseInteraction()) { cancelInteraction(false); return; }
    const elapsed = interactionTime ? clampStar(time - interactionTime, 0, 32) : 1000 / 60;
    interactionTime = time;
    // Navigation pauses layout, but dragging a star must pull its connected
    // spring network. Solve at most one step per 60Hz budget on this same RAF;
    // the pinned star still follows every display frame, including 120Hz input.
    const pulling = Boolean(drag?.moved && drag.id && starCanAnimate(activity) && engine.hot);
    if (pulling && time - lastDragPhysicsTime >= 1000 / 60 - 0.01) {
      lastDragPhysicsTime = time;
      engine.step(); positionsPending = true;
      frames++; attr(surface, "data-star-frames", String(frames));
    }
    if (cameraPending) {
      const amount = activity.quiet || drag ? 1 : 1 - Math.exp(-elapsed / 48);
      camera.x += (targetCamera.x - camera.x) * amount;
      camera.y += (targetCamera.y - camera.y) * amount;
      camera.k += (targetCamera.k - camera.k) * amount;
      if (Math.abs(targetCamera.x-camera.x)<0.02 && Math.abs(targetCamera.y-camera.y)<0.02 && Math.abs(targetCamera.k-camera.k)<0.00001) {
        camera = { ...targetCamera }; cameraPending = false;
      }
      // Exactly one parent transform per display frame; no node/font restyling.
      renderCamera(false);
    }
    if (positionsPending) { positionsPending = false; paintPositions(false); }
    if (selectionPending && (!navigationActive || Boolean(drag?.moved && drag.id))) { selectionPending = false; paintSelection(); }
    if (cameraPending || (pulling && engine.hot)) requestInteractionFrame();
    else if (inputIdle && !drag) finishInteraction();
  }
  function notifySelection(value: string | null) {
    selection = value;
    if (reportedSelection !== value) { reportedSelection = value; options.onSelect(value); }
  }
  const glyphFeedback = options.contextSafe((nodeId: string, scale: number) => {
    feedback?.kill();
    const glyph = options.nodes.get(nodeId)?.glyph;
    if (!glyph) return;
    if (!starCanAnimate(activity)) { glyph.setAttribute("transform", `scale(${scale})`); return; }
    const value = { scale: scale > 1 ? 1 : 1.32 };
    feedback = gsap.to(value, { scale, duration: 0.2, ease: "power2.out",
      onUpdate: () => { if (!destroyed) glyph.setAttribute("transform", `scale(${value.scale.toFixed(3)})`); } });
  });
  const nodeId = (target: EventTarget | null) => target instanceof Element ? target.closest<SVGGElement>("[data-star-node]")?.dataset.starNode || null : null;
  const pointerDown = (event: PointerEvent) => {
    if (event.button !== 0 || drag || !canUseInteraction()) return;
    const rect = surface.getBoundingClientRect();
    surfaceRect = rect;
    const point = starClientPoint(event.clientX, event.clientY, rect);
    if (!point) return;
    cancelInteraction(false);
    const target = nodeId(event.target);
    const node = target ? engine.byId.get(target) : null;
    const world = starWorldPoint(point, camera);
    drag = { pointer: event.pointerId, id: node?.id || null, x: event.clientX, y: event.clientY, moved: false,
      lastX: event.clientX, lastY: event.clientY,
      rect, camera: { ...camera }, offset: { x: node ? node.x - world.x : 0, y: node ? node.y - world.y : 0 } };
    suppressClick = false; hoverResumePoint = null;
    try { surface.setPointerCapture(event.pointerId); } catch { /* A removed surface cannot capture. */ }
    // A press is not a drag: don't restart physics or rerender the scene here.
    markInteraction(false); event.preventDefault();
  };
  const pointerMove = (event: PointerEvent) => {
    if (!canUseInteraction()) return;
    if (!drag) {
      // Releasing capture/physics movement can emit pointerover beneath a still
      // cursor. Restore hover only after the user deliberately moves again.
      if (hoverResumePoint && Math.hypot(event.clientX - hoverResumePoint.x, event.clientY - hoverResumePoint.y) > 4) {
        hoverResumePoint = null; hover(event);
      }
      return;
    }
    if (drag.pointer !== event.pointerId) return;
    const point = starClientPoint(event.clientX, event.clientY, drag.rect);
    if (!point) return;
    drag.lastX = event.clientX; drag.lastY = event.clientY;
    const starting = !drag.moved;
    drag.moved ||= Math.hypot(event.clientX - drag.x, event.clientY - drag.y) > 4;
    if (!drag.moved) return;
    if (starting && drag.id) {
      lastDragPhysicsTime = -Infinity;
      hovered = drag.id; selectionPending = true;
      if (starCanAnimate(activity)) status("settling");
    }
    markInteraction(true); surface.classList.add("is-dragging");
    cameraTouched = true;
    if (drag.id) {
      const world = starWorldPoint(point, camera);
      engine.pin(drag.id, world.x + drag.offset.x, world.y + drag.offset.y);
      // Only an actual drag replenishes the bounded cooling budget. Merely
      // clicking, hovering or navigating must never restart the simulation.
      if (starCanAnimate(activity)) engine.reheat(0.35);
      positionsPending = true;
    } else {
      const origin = starClientPoint(drag.x, drag.y, drag.rect)!;
      targetCamera = { ...drag.camera, x: clampStar(drag.camera.x + point.x - origin.x, -5000, 5000),
        y: clampStar(drag.camera.y + point.y - origin.y, -5000, 5000) };
      cameraPending = true;
    }
    requestInteractionFrame();
    event.preventDefault();
  };
  const endDrag = (cancelled = false) => {
    if (!drag) return;
    const previous = drag; drag = null; suppressClick = previous.moved;
    if (previous.moved) {
      if (cameraPending) { camera = { ...targetCamera }; cameraPending = false; renderCamera(false); }
      if (positionsPending) { positionsPending = false; paintPositions(false); }
      if (previous.id) {
        engine.release(previous.id, !cancelled && !activity.quiet);
        if (!activity.quiet) quietSettled = false;
        hovered = null; selectionPending = false;
        hoverResumePoint = { x: previous.lastX, y: previous.lastY };
        notifySelection(null);
      }
    }
    try { if (surface.hasPointerCapture(previous.pointer)) surface.releasePointerCapture(previous.pointer); } catch { /* Detached surface. */ }
    surface.classList.remove("is-dragging");
    if (!cancelled && previous.moved) {
      // Continue cooling immediately on release, not after the input-idle
      // timeout (which otherwise produces a visible stop-then-jump).
      if (interactionFrame) cancelAnimationFrame(interactionFrame);
      interactionFrame = 0; finishInteraction();
    } else if (!cancelled) markInteraction(false);
    synchronize();
  };
  const pointerUp = (event: PointerEvent) => {
    if (destroyed || !drag || drag.pointer !== event.pointerId) return;
    // The release event may carry a final position not delivered in pointermove.
    if (drag.moved && event.type === "pointerup") pointerMove(event);
    const completed = drag;
    endDrag(event.type !== "pointerup");
    if (event.type !== "pointerup") cancelInteraction(false);
    // Chrome retargets pointerup/click to the capture surface. The down target,
    // not event.target on release, is the authoritative node for this gesture.
    if (event.type === "pointerup" && !completed.moved && completed.id) {
      const node = engine.byId.get(completed.id);
      if (node) {
        suppressClick = true;
        notifySelection(node.id); paintSelection(); options.onOpen(node.item);
      }
    } else if (event.type !== "pointerup") suppressClick = true;
  };
  const click = (event: MouseEvent) => {
    if (!canUseInteraction()) return;
    const target = nodeId(event.target);
    if (suppressClick) {
      suppressClick = false;
      if (event.detail > 0 || "pointerId" in event) { event.preventDefault(); return; }
      // A keyboard/programmatic MouseEvent with detail=0 is a fresh activation.
    }
    const node = target ? engine.byId.get(target) : null;
    hoverResumePoint = null;
    markInteraction(false);
    if (node) { notifySelection(node.id); paintSelection(); options.onOpen(node.item); }
    else { hovered = null; notifySelection(null); paintSelection(); }
  };
  const hover = (event: PointerEvent) => {
    if (!drag && !navigationActive && !hoverResumePoint && canUseInteraction()) {
      const next = nodeId(event.target);
      if (next !== hovered) { hovered = next; selectionPending = true; markInteraction(false); requestInteractionFrame(); }
    }
  };
  const leave = () => { if (!drag) { hovered = null; selectionPending = true; requestInteractionFrame(); } };
  const wheel = (event: WheelEvent) => {
    if (!canUseInteraction() || drag) return;
    if (!navigationActive || !surfaceRect) surfaceRect = surface.getBoundingClientRect();
    const point = starClientPoint(event.clientX, event.clientY, surfaceRect);
    if (!point) return;
    const delta = normalizeStarWheel(event.deltaY, event.deltaMode, surfaceRect.height);
    if (!delta) return;
    event.preventDefault(); cameraTouched = true;
    if (!cameraPending) targetCamera = { ...camera };
    targetCamera = zoomStarCamera(targetCamera, Math.exp(-delta * 0.0025), point);
    cameraPending = true; markInteraction(true); requestInteractionFrame();
  };
  const visibility = () => { activity.visible = document.visibilityState !== "hidden"; if (!activity.visible) { endDrag(true); cancelInteraction(false); } synchronize(); };
  const blur = () => { activity.focused = false; endDrag(true); cancelInteraction(false); synchronize(); };
  const focus = () => { activity.focused = true; synchronize(); };
  const observer = typeof IntersectionObserver === "undefined" ? null : new IntersectionObserver((entries) => {
    if (destroyed) return;
    activity.onscreen = entries.some((entry) => entry.isIntersecting && entry.intersectionRatio > 0);
    if (!activity.onscreen) { endDrag(true); cancelInteraction(false); } else measureViewport(); synchronize();
  }, { threshold: [0, 0.02] });
  observer?.observe(surface);
  const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measureViewport);
  resize?.observe(surface);
  document.addEventListener("visibilitychange", visibility); window.addEventListener("blur", blur); window.addEventListener("focus", focus);
  surface.addEventListener("pointerdown", pointerDown); surface.addEventListener("pointermove", pointerMove);
  surface.addEventListener("pointerup", pointerUp); surface.addEventListener("pointercancel", pointerUp);
  surface.addEventListener("lostpointercapture", pointerUp); surface.addEventListener("pointerover", hover);
  surface.addEventListener("pointerleave", leave); surface.addEventListener("click", click); surface.addEventListener("wheel", wheel, { passive: false });
  measureViewport(); renderCamera(); paintPositions(); paintSelection(); synchronize();
  return {
    zoom: (factor) => {
      if (!canUseInteraction() || !Number.isFinite(factor) || factor <= 0) return;
      cameraTouched = true;
      if (!cameraPending) targetCamera = { ...camera };
      targetCamera = zoomStarCamera(targetCamera, factor); cameraPending = true;
      markInteraction(true); requestInteractionFrame();
    },
    fit: () => { if (destroyed) return; cancelInteraction(false); cameraTouched = true; fit(); },
    replay: () => { if (destroyed) return; cancelInteraction(false); engine.replay(); quietSettled = false; cameraTouched = false; fit(); paintPositions(); entrance.restart().pause(); synchronize(); },
    activity: (pause, reduce, nextLevel) => {
      if (destroyed) return;
      const wasQuiet = activity.quiet; activity.paused = pause; activity.quiet = reduce; level = nextLevel;
      if (wasQuiet && !reduce && !pause) engine.reheat(0.25);
      if (pause || reduce) { endDrag(); cancelInteraction(true); } synchronize();
    },
    theme: (value) => {
      if (destroyed || value === theme) return;
      paintCache = new WeakMap(); selectionSignature = "";
      theme = value; const nextKey = physicsSignature(theme);
      if (nextKey !== physicsKey) { physicsKey = nextKey; engine.configure(theme.physics); quietSettled = false; }
      paintSelection(); synchronize();
    },
    select: (value) => {
      if (destroyed) return;
      if (selection === value && reportedSelection === value) return;
      hoverResumePoint = null;
      reportedSelection = value; selection = value; markInteraction(false); paintSelection();
    },
    dispose: () => {
      if (destroyed) return;
      destroyed = true;
      // Cleanup may run while React keeps the SVG mounted (for example, a data
      // change). Release capture without endDrag's selection/paint/reheat work.
      const captured = drag; drag = null;
      if (captured?.id) engine.release(captured.id, false);
      try { if (captured && surface.hasPointerCapture(captured.pointer)) surface.releasePointerCapture(captured.pointer); } catch { /* Detached surface. */ }
      surface.classList.remove("is-dragging");
      cancelInteraction(false); detachClock(); engine.destroy(); entrance.kill(); feedback?.kill(); observer?.disconnect(); resize?.disconnect();
      document.removeEventListener("visibilitychange", visibility); window.removeEventListener("blur", blur); window.removeEventListener("focus", focus);
      surface.removeEventListener("pointerdown", pointerDown); surface.removeEventListener("pointermove", pointerMove);
      surface.removeEventListener("pointerup", pointerUp); surface.removeEventListener("pointercancel", pointerUp);
      surface.removeEventListener("lostpointercapture", pointerUp); surface.removeEventListener("pointerover", hover);
      surface.removeEventListener("pointerleave", leave); surface.removeEventListener("click", click); surface.removeEventListener("wheel", wheel);
    },
  };
}
