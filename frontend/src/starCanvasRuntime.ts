import gsap from "gsap";
import { graphGroup, type WikiGraphData, type WikiNode, type GraphRenderBudget } from "./knowledgeTypes";
import { nodeStarColor, type StarGraphTheme } from "./starGraphTheme";
import { clampStar, starRadius, type StarBounds } from "./starGraphPhysics";
import { starClientPoint, starWorldPoint, zoomStarCamera, normalizeStarWheel, type StarCamera, type StarPhase, type StarRuntime } from "./starGraphEngine";
import { createStarPhysicsClient } from "./starPhysicsClient";
import { createStarPainter, type StarPaintNode, type StarPaintEdge } from "./starPainter";
import { setGraphInteraction } from "./visualInteraction";

export function approachStar(current: number, target: number, elapsed: number, tau = 28, epsilon = .015) {
  return Math.abs(target - current) < epsilon ? target : current + (target - current) * (1 - Math.exp(-Math.min(50, elapsed) / tau));
}
export function canvasFit(bounds: StarBounds, width: number, height: number): StarCamera {
  const scale = Math.min(width / 960, height / 620);
  const margin = Math.min(72, Math.min(width, height) * .13) / Math.max(.1, scale);
  const k = clampStar(Math.min((width / scale - margin * 2) / Math.max(180, bounds.right - bounds.left),
    (height / scale - margin * 2) / Math.max(140, bounds.bottom - bounds.top)), .18, 1.5);
  return { k, x: 480 - (bounds.left + bounds.right) * k / 2, y: 310 - (bounds.top + bounds.bottom) * k / 2 };
}
export function hitCanvasStar(nodes: StarPaintNode[], point: { x: number; y: number }, camera: StarCamera, screenScale: number) {
  const world = starWorldPoint(point, camera), factor = Math.max(.01, camera.k * screenScale);
  let hit = -1, distance = Infinity;
  nodes.forEach((node, i) => {
    const d = Math.hypot(world.x - node.x, world.y - node.y) * factor;
    if (d <= Math.max(6, node.radius * factor + 2) && d < distance) { distance = d; hit = i; }
  });
  return hit;
}
function colorBetween(from: string, to: string, amount: number) {
  if (from === to || amount >= .99) return to;
  const a = parseInt(from.slice(1), 16), b = parseInt(to.slice(1), 16);
  let result = 0;
  for (const bit of [16, 8, 0]) {
    const start = (a >> bit) & 255, end = (b >> bit) & 255;
    const diff = end - start;
    result |= (diff === 0 ? end : start + Math.sign(diff) * Math.max(1, Math.round(Math.abs(diff) * amount))) << bit;
  }
  return `#${result.toString(16).padStart(6, "0")}`;
}
type Options = {
  canvas: HTMLCanvasElement; labels: HTMLCanvasElement; graph: WikiGraphData; focusId?: string; renderBudget?: GraphRenderBudget;
  theme: StarGraphTheme; paused: boolean; quiet: boolean; level: string; selection: string | null; diagnostics?: boolean;
  onOpen(node: WikiNode): void; onSelect(id: string | null): void; onPhase(phase: StarPhase): void;
  onBackend(value: string): void;
};
type Dependencies = {
  painterFactory?: typeof createStarPainter;
  physicsFactory?: typeof createStarPhysicsClient;
};

/** One display RAF for camera, drag, released cooling and visual feedback. */
export function mountStarCanvas(options: Options, dependencies: Dependencies = {}): StarRuntime {
  const { canvas, graph } = options;
  let theme = options.theme, paused = options.paused, quiet = options.quiet;
  let selected = options.selection, hovered: number = -1, reported = selected;
  let disposed = false, visible = document.visibilityState !== "hidden", focused = document.hasFocus();
  let onscreen = typeof IntersectionObserver === "undefined";
  let frame = 0, lastTime = 0, frames = 0, dirty = true, received = false, hot = false;
  let camera: StarCamera = { x: 480, y: 310, k: .6 }, targetCamera = { ...camera };
  let cameraTouched = false, cameraMoving = false, mode = "", backend = "worker", phase: StarPhase | null = null, backendLabel = "";
  let rect = canvas.getBoundingClientRect(), scale = Math.max(.1, Math.min(rect.width / 960, rect.height / 620));
  let bounds: StarBounds = { left: -400, right: 400, top: -240, bottom: 240 };
  let inputDeadline = 0, priority = false, suppressClick = false, resumeHover: { x: number; y: number } | null = null;
  let drag: { pointer: number; index: number; x: number; y: number; lastX: number; lastY: number; moved: boolean;
    camera: StarCamera; offset: { x: number; y: number }; world: { x: number; y: number } } | null = null;
  const owner = {};
  const ids = new Map(graph.nodes.map((node, i) => [node.id, i]));
  const adjacent = graph.nodes.map(() => new Set<number>());
  graph.edges.forEach(edge => { const a = ids.get(edge.source)!, b = ids.get(edge.target)!; adjacent[a].add(b); adjacent[b].add(a); });
  const center = ids.get(options.focusId || "") ?? adjacent.reduce((best, set, i) => set.size > adjacent[best].size ? i : best, 0);
  const nodes: StarPaintNode[] = graph.nodes.map((item, i) => ({ id: item.id, x: 0, y: 0,
    radius: starRadius(adjacent[i].size, theme.physics.nodeSize), color: nodeStarColor(theme, item.id, graphGroup(item)),
    opacity: 1, halo: 0, label: item.label, labelOpacity: 0 }));
  const edges: StarPaintEdge[] = graph.edges.map(edge => ({ source: ids.get(edge.source)!, target: ids.get(edge.target)!, color: theme.edge, opacity: .58, width: theme.physics.edgeWidth }));
  const positions = new Float32Array(nodes.length * 2);
  const labelTargets = new Float32Array(nodes.length);
  const ranked = [...nodes.keys()].sort((a, b) => adjacent[b].size - adjacent[a].size);
  const prominent = new Set(ranked.slice(0, 24)), labelRank = new Set(ranked.slice(0, 30));
  const samples = [0, 16, 233, 249, 250, 251, 267, 299].filter(i => i < nodes.length);
  const canDraw = () => !disposed && visible && focused && onscreen;
  const dynamic = () => canDraw() && !paused && !quiet;
  const setData = (key: string, value: string) => { if (canvas.dataset[key] !== value) canvas.dataset[key] = value; };
  const status = () => {
    const next = !canDraw() || paused ? "paused" : quiet ? "quiet" : hot ? "settling" : "settled";
    if (next !== phase) { phase = next; setData("starPhase", next); options.onPhase(next); }
  };
  const request = () => {
    if (!frame && canDraw()) {
      if (!lastTime) lastTime = performance.now();
      frame = requestAnimationFrame(paint);
    }
  };
  const invalidate = () => { dirty = true; request(); };
  const painter = (dependencies.painterFactory || createStarPainter)(canvas, options.labels, invalidate);
  function reportBackend() {
    setData("starRenderer", painter.mode); setData("starPhysics", backend);
    const next = `${painter.mode === "webgl" ? "WebGL" : "Canvas 2D"} · ${backend === "worker" ? "Worker" : "主线程备用"}`;
    if (backendLabel !== next) { backendLabel = next; options.onBackend(next); }
  }
  const physics = (dependencies.physicsFactory || createStarPhysicsClient)({ graph, physics: theme.physics, focusId: options.focusId, budget: options.renderBudget, initialMode: quiet ? "quiet" : "paused",
    onBackend(value) { backend = value; reportBackend(); },
    onFrame(value) {
      if (disposed) return;
      const wasHot = hot;
      positions.set(value.positions); bounds = value.bounds; hot = value.hot;
      if (!received || quiet) nodes.forEach((node, i) => { node.x = positions[2 * i]; node.y = positions[2 * i + 1]; });
      if (!received) { camera = canvasFit(bounds, rect.width, rect.height); targetCamera = { ...camera }; }
      else if (wasHot && !hot && !cameraTouched) { targetCamera = canvasFit(bounds, rect.width, rect.height); cameraMoving = true; }
      received = true; setData("starTicks", String(value.ticks));
      status(); invalidate();
    },
  });
  function syncPhysics() {
    const next = !canDraw() || paused || (cameraMoving && !drag) ? "paused" : quiet ? "quiet" : "running";
    if (next !== mode) { mode = next; physics.send({ type: "mode", mode: next }); }
    status();
  }
  function setPriority(value: boolean) {
    if (priority === value) return;
    priority = value; setGraphInteraction(owner, value); setData("starInteracting", String(value));
  }
  function notify(value: string | null) { selected = value; if (reported !== value) { reported = value; options.onSelect(value); } }
  function beginInput() { inputDeadline = performance.now() + 120; setPriority(true); invalidate(); }
  function paint(time: number) {
    frame = 0;
    if (!canDraw()) return;
    const dt = lastTime ? clampStar(time - lastTime, 1, 50) : 1000 / 60; lastTime = time;
    let moving = false, styling = false;
    const blend = quiet || paused ? 1 : 1 - Math.exp(-dt / 28);
    if (cameraMoving) {
      for (const axis of ["x", "y", "k"] as const) {
        camera[axis] = quiet || drag ? targetCamera[axis] : approachStar(camera[axis], targetCamera[axis], dt, 40, axis === "k" ? .00001 : .015);
      }
      cameraMoving = Math.abs(camera.x - targetCamera.x) > .02 || Math.abs(camera.y - targetCamera.y) > .02 || Math.abs(camera.k - targetCamera.k) > .0001;
      if (!cameraMoving) camera = { ...targetCamera };
      dirty = true;
    }
    const focusIndex = drag?.moved && drag.index >= 0 ? drag.index : hovered >= 0 ? hovered : ids.get(selected || "") ?? -1;
    const focusNeighbors = adjacent[focusIndex];
    for (let i = 0; i < nodes.length; i++) {
      const node = nodes[i];
      if (received) {
        const x = drag?.moved && drag.index === i ? drag.world.x : positions[2 * i];
        const y = drag?.moved && drag.index === i ? drag.world.y : positions[2 * i + 1];
        const nextX = quiet || paused || drag?.index === i && drag.moved ? x : approachStar(node.x, x, dt);
        const nextY = quiet || paused || drag?.index === i && drag.moved ? y : approachStar(node.y, y, dt);
        if (nextX !== node.x || nextY !== node.y) { node.x = nextX; node.y = nextY; moving = true; }
      }
      const connected = focusIndex < 0 || i === focusIndex || focusNeighbors?.has(i);
      const opacity = connected ? 1 : .24;
      const color = nodeStarColor(theme, node.id, graphGroup(graph.nodes[i]), i === focusIndex || (i === center && theme.mode === "group"));
      const opacityNext = quiet ? opacity : approachStar(node.opacity, opacity, dt, 45);
      const colorNext = colorBetween(node.color, color, blend);
      const labelTarget = theme.labels === "none" ? 0 : theme.labels === "all" ? 1 : i === focusIndex ? 1 :
        focusNeighbors?.has(i) && scale >= .55 ? .95 : nodes.length <= 16 ? 1 : labelRank.has(i) ? clampStar((camera.k * scale - .6) / .5, 0, 1) : 0;
      labelTargets[i] = labelTarget;
      if (node.opacity !== opacityNext || node.color !== colorNext) styling = true;
      node.opacity = opacityNext; node.color = colorNext;
      node.radius = Math.max(starRadius(adjacent[i].size, theme.physics.nodeSize), 2.15 * theme.physics.nodeSize / Math.max(.01, camera.k * scale));
      node.halo = prominent.has(i) ? .065 : 0;
    }
    if (theme.labels === "auto") {
      const occupied: { left: number; right: number; top: number; bottom: number }[] = [];
      const order = [...nodes.keys()].filter(i => labelTargets[i] > 0).sort((a, b) =>
        a === focusIndex ? -1 : b === focusIndex ? 1 : labelTargets[b] - labelTargets[a] || adjacent[b].size - adjacent[a].size);
      for (const i of order) {
        const node = nodes[i], x = (rect.width - 960 * scale) / 2 + (camera.x + node.x * camera.k) * scale;
        const y = (rect.height - 620 * scale) / 2 + (camera.y + node.y * camera.k) * scale;
        const width = Math.min(180, Array.from(node.label).length * 12);
        const gap = node.radius * camera.k * scale + 7;
        const left = x > rect.width / 2 ? x - gap - width : x + gap;
        const box = { left: left - 3, right: left + width + 3, top: y - 9, bottom: y + 9 };
        if (occupied.some(b => box.left < b.right && box.right > b.left && box.top < b.bottom && box.bottom > b.top)) labelTargets[i] = 0;
        else occupied.push(box);
      }
    }
    nodes.forEach((node, i) => {
      const next = quiet ? labelTargets[i] : approachStar(node.labelOpacity, labelTargets[i], dt, 65);
      if (next !== node.labelOpacity) styling = true;
      node.labelOpacity = next;
    });
    edges.forEach(edge => {
      const related = focusIndex >= 0 && (edge.source === focusIndex || edge.target === focusIndex);
      const opacity = focusIndex < 0 ? .58 : related ? .94 : .12;
      const color = related ? theme.highlight : theme.edge;
      const next = quiet ? opacity : approachStar(edge.opacity, opacity, dt, 45), nextColor = colorBetween(edge.color, color, blend);
      if (edge.opacity !== next || edge.color !== nextColor) styling = true;
      edge.opacity = next; edge.color = nextColor;
      edge.width = Math.max(theme.physics.edgeWidth, .45 * theme.physics.edgeWidth / Math.max(.01, camera.k * scale)) * (related ? 1.35 : 1);
    });
    if (received && (dirty || moving || styling)) {
      if (painter.draw({ nodes, edges, camera })) {
        reportBackend();
        frames++; setData("starFrames", String(frames));
        setData("starCamera", `${camera.x.toFixed(3)},${camera.y.toFixed(3)},${camera.k.toFixed(5)}`);
        setData("starDrawNodes", String(nodes.length)); setData("starDrawEdges", String(edges.length));
        setData("starSelected", selected || "");
        setData("starDimCount", String(nodes.filter(node => node.opacity < .98).length));
        if (options.diagnostics) {
          setData("starGeometry", samples.map(i => `${nodes[i].x.toFixed(3)},${nodes[i].y.toFixed(3)}`).join(";"));
          setData("starSamples", JSON.stringify(samples.map(i => ({ id: nodes[i].id, x: nodes[i].x, y: nodes[i].y, radius: nodes[i].radius, color: nodes[i].color }))));
        }
      }
      dirty = false;
    }
    if (!drag && !cameraMoving && time >= inputDeadline) setPriority(false);
    syncPhysics();
    if (moving || styling || cameraMoving || priority && !drag) request();
    else lastTime = 0; // A new gesture must not integrate time spent idle.
  }
  function measure() {
    if (disposed) return;
    rect = canvas.getBoundingClientRect(); scale = Math.max(.1, Math.min(rect.width / 960, rect.height / 620));
    painter.resize(rect.width, rect.height, Math.min(2, window.devicePixelRatio || 1));
    if (!cameraTouched && received) { camera = canvasFit(bounds, rect.width, rect.height); targetCamera = { ...camera }; }
    invalidate();
  }
  function point(event: MouseEvent) { return starClientPoint(event.clientX, event.clientY, rect); }
  function hoverAt(event: PointerEvent) {
    if (!canDraw() || drag || resumeHover) return;
    const p = point(event); if (!p) return;
    const next = hitCanvasStar(nodes, p, camera, scale);
    canvas.style.cursor = next >= 0 ? "pointer" : "grab";
    if (next !== hovered) { hovered = next; beginInput(); }
  }
  function down(event: PointerEvent) {
    if (!canDraw() || !received || drag || event.button !== 0) return;
    rect = canvas.getBoundingClientRect(); const p = point(event); if (!p) return;
    const index = hitCanvasStar(nodes, p, camera, scale), world = starWorldPoint(p, camera);
    drag = { pointer: event.pointerId, index, x: event.clientX, y: event.clientY, lastX: event.clientX, lastY: event.clientY,
      camera: { ...camera }, moved: false, world: { ...world }, offset: { x: index < 0 ? 0 : nodes[index].x - world.x, y: index < 0 ? 0 : nodes[index].y - world.y } };
    cameraMoving = false; targetCamera = { ...camera }; resumeHover = null; suppressClick = false;
    try { canvas.setPointerCapture(event.pointerId); } catch { /* Detached surface. */ }
    beginInput(); event.preventDefault();
  }
  function move(event: PointerEvent) {
    if (!canDraw()) return;
    if (!drag) {
      if (resumeHover && Math.hypot(event.clientX - resumeHover.x, event.clientY - resumeHover.y) > 4) resumeHover = null;
      hoverAt(event); return;
    }
    if (drag.pointer !== event.pointerId) return;
    const p = point(event); if (!p) return;
    drag.lastX = event.clientX; drag.lastY = event.clientY;
    drag.moved ||= Math.hypot(event.clientX - drag.x, event.clientY - drag.y) > 4;
    if (!drag.moved) return;
    cameraTouched = true; canvas.classList.add("is-dragging"); canvas.style.cursor = "grabbing";
    if (drag.index >= 0) {
      const world = starWorldPoint(p, camera); drag.world = { x: world.x + drag.offset.x, y: world.y + drag.offset.y };
      positions[2 * drag.index] = drag.world.x; positions[2 * drag.index + 1] = drag.world.y;
      physics.send({ type: "pin", id: nodes[drag.index].id, ...drag.world });
    } else {
      const origin = starClientPoint(drag.x, drag.y, rect)!;
      camera = { ...drag.camera, x: clampStar(drag.camera.x + p.x - origin.x, -5000, 5000), y: clampStar(drag.camera.y + p.y - origin.y, -5000, 5000) };
      targetCamera = { ...camera };
    }
    syncPhysics(); beginInput(); event.preventDefault();
  }
  function finishDrag(cancelled = false) {
    if (!drag) return;
    const previous = drag; drag = null; suppressClick = previous.moved || cancelled;
    if (previous.moved && previous.index >= 0) {
      physics.send({ type: "release", id: nodes[previous.index].id, reheat: !cancelled && !quiet });
      hovered = -1; notify(null); resumeHover = { x: previous.lastX, y: previous.lastY };
    }
    try { if (canvas.hasPointerCapture(previous.pointer)) canvas.releasePointerCapture(previous.pointer); } catch { /* Detached. */ }
    canvas.classList.remove("is-dragging"); canvas.style.cursor = "grab";
    if (!cancelled && !previous.moved && previous.index >= 0) {
      notify(nodes[previous.index].id); options.onOpen(graph.nodes[previous.index]); suppressClick = true;
    } else if (!cancelled && !previous.moved) {
      hovered = -1; notify(null); resumeHover = null;
    }
    setPriority(false); syncPhysics(); invalidate();
  }
  function up(event: PointerEvent) {
    if (!drag || drag.pointer !== event.pointerId) return;
    if (drag.moved && event.type === "pointerup") move(event);
    finishDrag(event.type !== "pointerup");
  }
  function wheel(event: WheelEvent) {
    if (!canDraw() || drag || !received) return;
    rect = canvas.getBoundingClientRect(); const p = point(event); if (!p) return;
    const delta = normalizeStarWheel(event.deltaY, event.deltaMode, rect.height); if (!delta) return;
    event.preventDefault(); cameraTouched = true;
    targetCamera = zoomStarCamera(targetCamera, Math.exp(-delta * .0025), p); cameraMoving = true;
    beginInput(); syncPhysics();
  }
  function click(event: MouseEvent) {
    if (!canDraw()) return;
    if (suppressClick) { suppressClick = false; if (event.detail > 0 || "pointerId" in event) return; }
    if (event.detail !== 0) return; // Pointer activation is resolved from the captured down target.
    const p = point(event), index = p ? hitCanvasStar(nodes, p, camera, scale) : -1;
    if (index >= 0) { resumeHover = null; notify(nodes[index].id); options.onOpen(graph.nodes[index]); invalidate(); }
  }
  function leave() { if (!drag) { hovered = -1; canvas.style.cursor = "grab"; invalidate(); } }
  function key(event: KeyboardEvent) {
    if (!canDraw()) return;
    const zoom = event.key === "+" || event.key === "=" ? 1.2 : event.key === "-" ? 1 / 1.2 : 0;
    const pan = ({ ArrowLeft: [-24, 0], ArrowRight: [24, 0], ArrowUp: [0, -24], ArrowDown: [0, 24] } as Record<string, number[]>)[event.key];
    if (event.key === "Escape") { finishDrag(true); hovered = -1; notify(null); invalidate(); }
    else if (zoom) { targetCamera = zoomStarCamera(targetCamera, zoom); cameraMoving = true; cameraTouched = true; beginInput(); }
    else if (pan) { targetCamera = { ...targetCamera, x: targetCamera.x + pan[0], y: targetCamera.y + pan[1] }; cameraMoving = true; cameraTouched = true; beginInput(); }
    else return;
    event.preventDefault(); syncPhysics();
  }
  function suspend() { finishDrag(true); if (frame) cancelAnimationFrame(frame); frame = 0; lastTime = 0; setPriority(false); syncPhysics(); }
  function visibility() { visible = document.visibilityState !== "hidden"; if (!visible) suspend(); else { syncPhysics(); invalidate(); } }
  function blur() { focused = false; suspend(); }
  function focus() { focused = true; syncPhysics(); invalidate(); }
  const intersection = typeof IntersectionObserver === "undefined" ? null : new IntersectionObserver(entries => {
    if (disposed) return; onscreen = entries.some(entry => entry.isIntersecting && entry.intersectionRatio > 0);
    if (!onscreen) suspend(); else { measure(); syncPhysics(); }
  });
  const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
  intersection?.observe(canvas); resize?.observe(canvas);
  const events = { pointerdown: down, pointermove: move, pointerup: up, pointercancel: up, lostpointercapture: up,
    pointerleave: leave, click, keydown: key, wheel };
  for (const [name, handler] of Object.entries(events)) canvas.addEventListener(name, handler as EventListener, name === "wheel" ? { passive: false } : undefined);
  document.addEventListener("visibilitychange", visibility); window.addEventListener("blur", blur); window.addEventListener("focus", focus);
  const entrance = gsap.timeline({ paused: true }).fromTo([canvas, options.labels], { opacity: .35 }, { opacity: 1, duration: .24, ease: "power2.out", immediateRender: false });
  if (quiet) entrance.progress(1); else entrance.play();
  measure(); syncPhysics(); reportBackend();
  return {
    zoom(factor) { if (!canDraw() || !Number.isFinite(factor) || factor <= 0) return; cameraTouched = true; targetCamera = zoomStarCamera(targetCamera, factor); cameraMoving = true; beginInput(); syncPhysics(); },
    fit() { if (disposed) return; cameraTouched = true; targetCamera = canvasFit(bounds, rect.width, rect.height); cameraMoving = true; beginInput(); },
    replay() { if (disposed) return; finishDrag(true); cameraTouched = false; physics.send({ type: "replay" }); invalidate(); },
    activity(pause, reduce) { if (disposed) return; paused = pause; quiet = reduce; if (pause || reduce) { finishDrag(true); entrance.progress(1).pause(); } syncPhysics(); invalidate(); },
    theme(value) { if (disposed || value === theme) return; const old = JSON.stringify(theme.physics); theme = value;
      if (old !== JSON.stringify(value.physics)) physics.send({ type: "configure", physics: value.physics }); invalidate(); },
    select(value) { if (disposed || selected === value && reported === value) return; selected = reported = value; hovered = -1; resumeHover = null; invalidate(); },
    dispose() {
      if (disposed) return; disposed = true;
      const captured = drag; drag = null;
      try { if (captured && canvas.hasPointerCapture(captured.pointer)) canvas.releasePointerCapture(captured.pointer); } catch { /* Detached. */ }
      if (frame) cancelAnimationFrame(frame); frame = 0; setPriority(false);
      physics.dispose(); painter.dispose(); entrance.kill(); intersection?.disconnect(); resize?.disconnect();
      for (const [name, handler] of Object.entries(events)) canvas.removeEventListener(name, handler as EventListener);
      document.removeEventListener("visibilitychange", visibility); window.removeEventListener("blur", blur); window.removeEventListener("focus", focus);
      canvas.classList.remove("is-dragging");
    },
  };
}
