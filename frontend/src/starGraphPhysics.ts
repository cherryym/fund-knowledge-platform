import { forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY,
  type Simulation, type SimulationLinkDatum, type SimulationNodeDatum } from "d3-force";
import { boundedGraph, graphGroup, type GraphRenderBudget, type WikiEdge, type WikiGraphData, type WikiNode } from "./knowledgeTypes";
import { normalizeStarPhysics, type StarPhysics } from "./starPhysicsSettings";
import type { StarPhysicsClock, StarPhysicsEvent, StarPhysicsMode, StarPhysicsSession } from "./starPhysicsProtocol";

export const STAR_WIDTH = 960;
export const STAR_HEIGHT = 620;
export const STAR_MAX_TICKS = 240;
export const STAR_STATIC_TICKS = 100;
export type StarGroup = ReturnType<typeof graphGroup>;
export type StarNode = SimulationNodeDatum & { id: string; item: WikiNode; group: StarGroup; degree: number; radius: number;
  x: number; y: number; vx: number; vy: number; phase: number; previousX: number; previousY: number };
export type StarLink = SimulationLinkDatum<StarNode> & { id: string; item: WikiEdge; source: StarNode; target: StarNode };
export type StarCamera = { x: number; y: number; k: number };
export type StarBounds = { left: number; right: number; top: number; bottom: number };
export function clampStar(value: number, min: number, max: number): number {
  return Number.isFinite(value) ? Math.max(min, Math.min(max, value)) : min;
}
export function starRadius(degree: number, size = 1): number { return (3.7 + Math.min(4.1, Math.sqrt(degree) * 1.1)) * size; }
function hash(id: string): number { let value = 2166136261; for (const char of id) value = Math.imul(value ^ char.charCodeAt(0), 16777619); return value >>> 0; }

export function prepareStarGraph(data: WikiGraphData, _budget: GraphRenderBudget = "standard"): WikiGraphData {
  if (!data || !Array.isArray(data.nodes) || !Array.isArray(data.edges)) throw new Error("图谱服务返回的数据结构无效。");
  const nodes = data.nodes.filter((node) => node && typeof node.id === "string" && !!node.id && node.id.length <= 128 && typeof node.label === "string");
  const edges = data.edges.filter((edge) => edge && typeof edge.id === "string" && !!edge.id && typeof edge.source === "string" && typeof edge.target === "string");
  return boundedGraph({ ...data, nodes, edges, truncated: data.truncated || nodes.length !== data.nodes.length || edges.length !== data.edges.length });
}

/** Extracted d3 model. D3's internal timer is stopped; only step() advances it. */
export class StarGraphEngine {
  readonly graph: WikiGraphData;
  readonly nodes: StarNode[];
  readonly links: StarLink[];
  readonly byId: Map<string, StarNode>;
  readonly adjacent = new Map<string, Set<string>>();
  readonly centerId?: string;
  private simulation: Simulation<StarNode, StarLink>;
  private physics: StarPhysics;
  private spent = 0;
  private disposed = false;
  totalTicks = 0;

  constructor(data: WikiGraphData, options?: Partial<StarPhysics>, focusId?: string, budget: GraphRenderBudget = "standard") {
    this.graph = prepareStarGraph(data, budget);
    this.physics = normalizeStarPhysics(options);
    for (const node of this.graph.nodes) this.adjacent.set(node.id, new Set());
    for (const edge of this.graph.edges) {
      this.adjacent.get(edge.source)?.add(edge.target); this.adjacent.get(edge.target)?.add(edge.source);
    }
    this.nodes = this.graph.nodes.map((item) => {
      const degree = this.adjacent.get(item.id)?.size || 0;
      return { id: item.id, item, group: graphGroup(item), degree, radius: starRadius(degree, this.physics.nodeSize),
        x: 0, y: 0, vx: 0, vy: 0, previousX: 0, previousY: 0, phase: (hash(item.id) % 628) / 100 };
    });
    this.byId = new Map(this.nodes.map((node) => [node.id, node]));
    this.centerId = this.byId.has(focusId || "") ? focusId : [...this.nodes].sort((a, b) => b.degree - a.degree)[0]?.id;
    this.links = this.graph.edges.map((item) => ({ id: item.id, item, source: this.byId.get(item.source)!, target: this.byId.get(item.target)! }));
    this.seed();
    this.simulation = forceSimulation<StarNode>(this.nodes).stop().alphaDecay(0.037).velocityDecay(this.physics.damping).alphaMin(0.002);
    this.configure(this.physics);
  }
  private seed() {
    const scale = this.nodes.length <= 12 ? 55 : 26;
    this.nodes.forEach((node, index) => {
      const angle = index * 2.399963229728653 + node.phase * 0.1;
      const radius = node.id === this.centerId ? 0 : 45 + scale * Math.sqrt(index + 1);
      node.x = Math.cos(angle) * radius;
      node.y = Math.sin(angle) * radius * 0.68;
      node.previousX = node.x; node.previousY = node.y;
      node.vx = 0; node.vy = 0; node.fx = null; node.fy = null;
    });
  }
  get hot(): boolean { return !this.disposed && this.nodes.length > 0 && this.spent < STAR_MAX_TICKS && this.simulation.alpha() > 0.002; }
  get phaseTicks(): number { return this.spent; }
  get destroyed(): boolean { return this.disposed; }
  configure(options: Partial<StarPhysics>) {
    if (this.disposed) return;
    this.physics = normalizeStarPhysics(options);
    this.nodes.forEach((node) => { node.radius = starRadius(node.degree, this.physics.nodeSize); });
    this.simulation.velocityDecay(this.physics.damping)
      .force("charge", forceManyBody<StarNode>().strength(-this.physics.repel).distanceMin(18).distanceMax(650))
      .force("links", forceLink<StarNode, StarLink>(this.links).id((node) => node.id).distance(this.physics.distance).strength(this.physics.linkStrength))
      .force("collide", forceCollide<StarNode>().radius((node) => node.radius + (this.nodes.length <= 20 ? 24 : 12)).iterations(1))
      .force("x", forceX<StarNode>(0).strength(this.physics.center))
      .force("y", forceY<StarNode>(0).strength(this.physics.center * 1.35));
    this.reheat(0.65);
  }
  step(iterations = 1): boolean {
    let moved = false;
    const count = Math.floor(clampStar(iterations, 0, STAR_MAX_TICKS));
    for (let i = 0; i < count && this.hot; i++) {
      for (const node of this.nodes) { node.previousX = node.x; node.previousY = node.y; }
      this.simulation.tick(); this.spent++; this.totalTicks++; moved = true;
      for (const node of this.nodes) {
        if (node.fx == null) node.x = node.previousX + clampStar(node.x - node.previousX, -10, 10);
        if (node.fy == null) node.y = node.previousY + clampStar(node.y - node.previousY, -10, 10);
        node.x = clampStar(node.x, -1100, 1100); node.y = clampStar(node.y, -750, 750);
        node.vx = clampStar(node.vx, -12, 12); node.vy = clampStar(node.vy, -12, 12);
      }
    }
    return moved;
  }
  settle(iterations = STAR_STATIC_TICKS) {
    if (this.disposed) return;
    this.step(Math.min(STAR_STATIC_TICKS, iterations));
    this.simulation.alpha(0).stop();
  }
  reheat(alpha = 0.28) {
    if (this.disposed) return;
    this.spent = 0;
    this.simulation.alpha(Math.max(this.simulation.alpha(), clampStar(alpha, 0, 1))).alphaTarget(0).stop();
  }
  replay() { if (!this.disposed) { this.seed(); this.spent = 0; this.simulation.alpha(1).alphaTarget(0).stop(); } }
  pin(id: string, x: number, y: number) {
    const node = this.byId.get(id);
    if (!node || this.disposed) return;
    node.x = node.fx = clampStar(x, -1100, 1100); node.y = node.fy = clampStar(y, -750, 750);
    node.previousX = node.x; node.previousY = node.y;
    node.vx = 0; node.vy = 0;
  }
  release(id: string, reheat = true) {
    if (this.disposed) return;
    const node = this.byId.get(id);
    if (node) { node.fx = null; node.fy = null; if (reheat) this.reheat(0.2); }
  }
  bounds(): StarBounds {
    if (!this.nodes.length) return { left: -200, right: 200, top: -150, bottom: 150 };
    const bounds = { left: Infinity, right: -Infinity, top: Infinity, bottom: -Infinity };
    for (const node of this.nodes) {
      bounds.left = Math.min(bounds.left, node.x - node.radius); bounds.right = Math.max(bounds.right, node.x + node.radius);
      bounds.top = Math.min(bounds.top, node.y - node.radius); bounds.bottom = Math.max(bounds.bottom, node.y + node.radius);
    }
    return bounds;
  }
  destroy() { this.disposed = true; this.simulation.stop().alpha(0); }
}

const STEP_MS = 1000 / 60;
const EPSILON_MS = 1e-7;
const defaultClock: StarPhysicsClock = {
  now: () => performance.now(),
  setTimeout: (callback, delay) => globalThis.setTimeout(callback, delay),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

/** No DOM/IO: the same session can run in a Worker or in a main-thread fallback. */
export function createStarPhysicsSession(emit: (event: StarPhysicsEvent) => void, clock: StarPhysicsClock = defaultClock): StarPhysicsSession {
  let engine: StarGraphEngine | undefined;
  let generation = -1;
  let sequence = 0;
  let mode: StarPhysicsMode = "paused";
  let disposed = false;
  let timer: { handle?: unknown } | undefined;
  let lastTime: number | undefined;
  let remainder = 0;

  function cancelWork() {
    const pending = timer;
    timer = undefined; lastTime = undefined; remainder = 0;
    if (pending) clock.clearTimeout(pending.handle);
  }
  function publish() {
    if (!engine || disposed) return;
    // Never retain a transferred array or expose the model's own coordinates.
    const positions = new Float32Array(engine.nodes.length * 2);
    engine.nodes.forEach((node, index) => { positions[index * 2] = node.x; positions[index * 2 + 1] = node.y; });
    emit({ type: "frame", generation, sequence: ++sequence, positions, ticks: engine.totalTicks,
      hot: mode === "running" && engine.hot, bounds: engine.bounds() });
  }
  function fail(error: unknown) {
    cancelWork(); engine?.destroy(); engine = undefined;
    if (!disposed) emit({ type: "error", generation, message: error instanceof Error ? error.message : String(error) });
  }
  function schedule() {
    if (disposed || timer || mode !== "running" || !engine?.hot) return;
    if (lastTime === undefined) lastTime = clock.now();
    const pending: { handle?: unknown } = {};
    const scheduledGeneration = generation;
    timer = pending;
    pending.handle = clock.setTimeout(() => {
      // Token identity also rejects callbacks already queued when clearTimeout ran.
      if (disposed || timer !== pending || generation !== scheduledGeneration) return;
      timer = undefined;
      if (mode !== "running" || !engine?.hot) { cancelWork(); return; }
      try {
        const now = clock.now();
        const elapsed = Math.max(0, now - (lastTime ?? now));
        lastTime = now;
        const accumulated = remainder + elapsed;
        const available = Math.floor((accumulated + EPSILON_MS) / STEP_MS);
        // Drop overdue whole steps after a stall, but retain the fractional step.
        remainder = Math.max(0, accumulated - available * STEP_MS);
        if (available > 0 && engine.step(Math.min(2, available))) publish();
        if (generation !== scheduledGeneration || disposed) return;
        if (!engine?.hot) cancelWork();
        else schedule();
      } catch (error) { fail(error); }
    }, Math.max(0, STEP_MS - remainder));
  }
  function dispose() {
    if (disposed) return;
    disposed = true; cancelWork(); engine?.destroy(); engine = undefined;
  }

  return { dispose, send(command) {
    if (disposed || !command || !Number.isSafeInteger(command.generation) || command.generation < 0) return;
    if (command.type === "init") {
      if (command.generation <= generation) return;
      cancelWork(); engine?.destroy(); engine = undefined;
      generation = command.generation; sequence = 0;
      try {
        if (!["running", "paused", "quiet"].includes(command.mode)) throw new Error("星图物理模式无效。");
        mode = command.mode;
        engine = new StarGraphEngine(command.graph, command.physics, command.focusId, command.budget);
        if (mode === "quiet") engine.settle();
        publish(); schedule();
      } catch (error) { fail(error); }
      return;
    }
    if (command.type === "configure") {
      if (!engine || command.generation <= generation) return;
      cancelWork(); generation = command.generation; sequence = 0;
      try {
        engine.configure(command.physics);
        // A settings change in a static mode must not move the existing layout.
        if (mode === "quiet") engine.settle(0);
        publish(); schedule();
      } catch (error) { fail(error); }
      return;
    }
    if (command.generation !== generation) return;
    if (command.type === "dispose") { dispose(); return; }
    if (!engine) return;
    try {
      switch (command.type) {
        case "mode":
          if (!["running", "paused", "quiet"].includes(command.mode)) throw new Error("星图物理模式无效。");
          if (mode === command.mode) return;
          cancelWork(); mode = command.mode;
          // Resume existing heat only: neither reseed nor settle on mode changes.
          break;
        case "pin":
          if (!engine.byId.has(command.id) || !Number.isFinite(command.x) || !Number.isFinite(command.y)) return;
          engine.pin(command.id, command.x, command.y);
          if (mode === "running") engine.reheat(0.35);
          break;
        case "release":
          if (!engine.byId.has(command.id)) return;
          engine.release(command.id, mode === "running" && command.reheat);
          break;
        case "replay":
          cancelWork(); engine.replay();
          // Explicit replay may request another bounded static layout.
          if (mode === "quiet") engine.settle();
          break;
        default: return;
      }
      publish(); schedule();
    } catch (error) { fail(error); }
  } };
}
