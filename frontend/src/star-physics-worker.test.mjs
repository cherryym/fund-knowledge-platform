// Real d3 and a real worker_threads transport. Only the session's clock and the
// Worker global/message bridge are adapted; no physics or transfer is mocked.
import test from "node:test";
import assert from "node:assert/strict";
import { Worker } from "node:worker_threads";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const frontend = fileURLToPath(new URL("../", import.meta.url));
const [modelBuild, workerBuild] = await Promise.all([
  build({ absWorkingDir: frontend,
    stdin: { contents: `export * from './src/starGraphPhysics'; export * from './src/starPhysicsSettings';
      export { createStarPhysicsSession as protocolFactory } from './src/starPhysicsProtocol';
      export { createStarfieldFixture } from './src/starfieldFixture';`, resolveDir: frontend, loader: "ts" },
    bundle: true, write: false, platform: "node", format: "esm", metafile: true }),
  build({ absWorkingDir: frontend, entryPoints: ["src/starPhysics.worker.ts"],
    bundle: true, write: false, platform: "browser", format: "iife", metafile: true }),
]);
const physics = await import(`data:text/javascript;base64,${Buffer.from(modelBuild.outputFiles[0].text).toString("base64")}`);
const { StarGraphEngine, createStarPhysicsSession, normalizeStarPhysics, STAR_DEFAULT_PHYSICS,
  STAR_PHYSICS_LIMITS, STAR_MAX_TICKS, STAR_STATIC_TICKS, prepareStarGraph, createStarfieldFixture } = physics;
const node = (id, extra = {}) => ({ id, label: `节点 ${id}`, kind: "knowledge", knowledge_type: "rule",
  category: "测试", state: "APPROVED", version_id: null, ...extra });
const edge = (id, source, target) => ({ id, source, target, type: "WIKI_LINK", origin: "wikilink", state: "ACTIVE" });
const graph = { nodes: [node("a"), node("b", { kind: "document" }), node("c", { knowledge_type: "term" })],
  edges: [edge("ab", "a", "b"), edge("bc", "b", "c")], truncated: false, total_visible_nodes: 3 };
const coords = (engine) => engine.nodes.flatMap(({ x, y }) => [x, y]);
const init = (generation = 1, mode = "running", data = graph) => ({ type: "init", generation, mode,
  graph: data, physics: { ...STAR_DEFAULT_PHYSICS } });

class ManualClock {
  time = 0;
  nextId = 0; // Verify that an opaque, falsy timer handle can still be cancelled.
  jobs = new Map();
  cancelled = [];
  now = () => this.time;
  setTimeout = (callback, milliseconds) => {
    const id = this.nextId++;
    this.jobs.set(id, { callback, due: this.time + milliseconds });
    return id;
  };
  clearTimeout = (id) => {
    const job = this.jobs.get(id);
    if (job) this.cancelled.push(job.callback);
    this.jobs.delete(id);
  };
  get first() { return [...this.jobs.entries()].sort((a, b) => a[1].due - b[1].due)[0]; }
  fireAt(time) {
    const pending = this.first;
    assert.ok(pending, "a timer must be scheduled");
    this.jobs.delete(pending[0]); this.time = time; pending[1].callback();
  }
  advance(milliseconds) {
    const end = this.time + milliseconds;
    let calls = 0;
    while (this.first && this.first[1].due <= end + 1e-8) {
      assert.ok(++calls < 10000, "timer must not spin without consuming elapsed time");
      this.fireAt(this.first[1].due);
    }
    this.time = end;
  }
}
function harness(t, command = init()) {
  const clock = new ManualClock();
  const events = [];
  const session = createStarPhysicsSession((event) => events.push(event), clock);
  t.after(() => { session.dispose(); assert.equal(clock.jobs.size, 0); });
  const send = (value) => session.send({ generation: command?.generation ?? 1, ...value });
  if (command) session.send(command);
  return { clock, events, session, send, frame: () => events.filter((event) => event.type === "frame").at(-1) };
}

test("pure model and browser worker bundle actual d3 without UI, DOM adapters or network dependencies", () => {
  assert.equal(physics.protocolFactory, createStarPhysicsSession);
  for (const result of [modelBuild, workerBuild]) {
    const inputs = Object.keys(result.metafile.inputs).join("\n");
    assert.match(inputs, /node_modules\/d3-force\/src\/simulation\.js/);
    assert.doesNotMatch(inputs, /(?:react|gsap|starGraphTheme|starGraphEngine|visualInteraction)/i);
    assert.equal(Object.values(result.metafile.outputs).flatMap((output) => output.imports).length, 0);
    // "document" is also a legitimate knowledge-node kind, not a DOM access.
    assert.equal(/\bdocument\s*[.\[]|\b(?:fetch|XMLHttpRequest|WebSocket)\s*\(/.test(result.outputFiles[0].text), false);
  }
});

test("settings preserve saved theme v1 defaults, limits and finite-number validation", () => {
  assert.deepEqual(STAR_DEFAULT_PHYSICS, { nodeSize: 1, edgeWidth: 0.85, center: 0.075, repel: 135, distance: 100, linkStrength: 0.22, damping: 0.52 });
  assert.deepEqual(STAR_PHYSICS_LIMITS, { nodeSize: [0.65, 1.8, 0.05], edgeWidth: [0.4, 2.4, 0.05],
    center: [0.02, 0.2, 0.005], repel: [35, 320, 5], distance: [45, 180, 5], linkStrength: [0.02, 0.8, 0.01], damping: [0.15, 0.85, 0.01] });
  for (const value of [null, [], false, "100", { repel: NaN, distance: Infinity, center: "0.1" }])
    assert.deepEqual(normalizeStarPhysics(value), STAR_DEFAULT_PHYSICS);
  assert.deepEqual(normalizeStarPhysics({ nodeSize: -1, edgeWidth: 10, center: 0, repel: 999, distance: -999, extra: 1 }),
    { nodeSize: 0.65, edgeWidth: 2.4, center: 0.02, repel: 320, distance: 45, linkStrength: 0.22, damping: 0.52 });
  const copy = normalizeStarPhysics(); copy.distance = 80;
  assert.equal(normalizeStarPhysics().distance, 100);
});

test("preparation preserves node order and removes invalid, duplicate and dangling entries", () => {
  const prepared = prepareStarGraph({ ...graph,
    nodes: [node("c"), null, node("a"), node("a"), node(""), node("b"), node("x".repeat(129))],
    edges: [edge("bc", "b", "c"), null, edge("bc", "b", "c"), edge("secret", "a", "missing")] });
  assert.deepEqual(prepared.nodes.map((item) => item.id), ["c", "a", "b"]);
  assert.deepEqual(prepared.edges.map((item) => item.id), ["bc"]);
  assert.equal(prepared.truncated, true);
  for (const value of [null, {}, { nodes: null, edges: [] }]) assert.throws(() => prepareStarGraph(value), /数据结构无效/);
});

test("normal and synthetic data both retain the complete graph without 200/800 limits", () => {
  const fixture = createStarfieldFixture(300);
  assert.equal(prepareStarGraph(fixture).nodes.length, 300);
  const synthetic = prepareStarGraph(fixture, "synthetic-300");
  assert.equal(synthetic.nodes.length, 300); assert.equal(synthetic.edges.length, 600);
  const dense = { ...fixture, edges: Array.from({ length: 1200 }, (_, i) => edge(`dense-${i}`, `perf-${i % 199}`, `perf-${(i + 1) % 199}`)) };
  assert.equal(prepareStarGraph(dense).edges.length, 1200);
});

test("real d3 is deterministic, keeps source data intact and never advances autonomously", async (t) => {
  const original = structuredClone(graph);
  const first = new StarGraphEngine(graph), second = new StarGraphEngine(graph);
  t.after(() => { first.destroy(); second.destroy(); });
  const seed = coords(first);
  assert.deepEqual(seed, coords(second));
  assert.deepEqual(first.nodes.map((item) => item.group), ["wiki", "source", "term"]);
  assert.equal(first.centerId, "b");
  await delay(40);
  assert.equal(first.totalTicks, 0); assert.deepEqual(coords(first), seed);
  first.step(25); second.step(25);
  assert.notDeepEqual(coords(first), seed); assert.deepEqual(coords(first), coords(second));
  assert.deepEqual(graph, original);
});

test("d3 cooling, displacement, velocity and world bounds remain finite and bounded", (t) => {
  const engine = new StarGraphEngine(createStarfieldFixture(300), { repel: 1e30, center: -999, nodeSize: NaN }, undefined, "synthetic-300");
  t.after(() => engine.destroy());
  while (engine.hot) {
    const before = coords(engine);
    engine.step();
    engine.nodes.forEach((item, i) => {
      assert.ok(Math.abs(item.x - before[i * 2]) <= 10.00001);
      assert.ok(Math.abs(item.y - before[i * 2 + 1]) <= 10.00001);
      assert.ok(Math.abs(item.x) <= 1100 && Math.abs(item.y) <= 750);
      assert.ok(Math.abs(item.vx) <= 12 && Math.abs(item.vy) <= 12);
    });
    assert.ok(engine.totalTicks <= STAR_MAX_TICKS);
  }
  const final = coords(engine); engine.step(10000);
  assert.deepEqual(coords(engine), final);
  const bounds = engine.bounds();
  for (const item of engine.nodes) assert.ok(item.x - item.radius >= bounds.left && item.x + item.radius <= bounds.right
    && item.y - item.radius >= bounds.top && item.y + item.radius <= bounds.bottom);
});

test("pin/release/replay preserve d3 ownership and a destroyed model cannot be changed", (t) => {
  const engine = new StarGraphEngine(graph);
  t.after(() => engine.destroy());
  const seed = coords(engine);
  engine.pin("a", 420, 130); engine.step(8);
  assert.deepEqual([engine.byId.get("a").x, engine.byId.get("a").y], [420, 130]);
  engine.release("a"); engine.step(8);
  assert.notEqual(engine.byId.get("a").x, 420);
  const ticks = engine.totalTicks;
  engine.replay(); assert.deepEqual(coords(engine), seed); assert.equal(engine.totalTicks, ticks);
  assert.ok(engine.nodes.every((item) => item.fx == null && item.fy == null && item.previousX === item.x));
  engine.destroy(); engine.pin("a", 999, 999); engine.configure({ repel: 35 }); engine.replay(); engine.release("a");
  assert.equal(engine.step(), false); assert.deepEqual(coords(engine), seed); assert.equal(engine.destroyed, true);
});

test("60Hz clock retains sub-step time and emits no fictitious ticks on an early wakeup", (t) => {
  const h = harness(t);
  assert.equal(h.frame().ticks, 0); assert.equal(h.frame().sequence, 1);
  h.clock.fireAt(10);
  assert.equal(h.events.length, 1);
  assert.ok(Math.abs(h.clock.first[1].due - 1000 / 60) < 1e-6);
  h.clock.fireAt(20); assert.equal(h.frame().ticks, 1);
  assert.ok(Math.abs(h.clock.first[1].due - 1000 / 30) < 1e-6);
  h.clock.fireAt(50); assert.equal(h.frame().ticks, 3);
  h.clock.advance(950); assert.equal(h.frame().ticks, 60);
  assert.deepEqual(h.events.map((frame) => frame.sequence), Array.from({ length: h.events.length }, (_, i) => i + 1));
});

test("a delayed callback catches up at most two steps and retains only fractional debt", (t) => {
  const h = harness(t);
  h.clock.fireAt(1007);
  assert.equal(h.frame().ticks, 2);
  assert.ok(Math.abs(h.clock.first[1].due - (1000 + 1000 / 60)) < 1e-6);
  h.clock.fireAt(h.clock.first[1].due); assert.equal(h.frame().ticks, 3);
  h.clock.fireAt(h.clock.first[1].due); assert.equal(h.frame().ticks, 4);
});

test("running session reaches a cold terminal frame and leaves no further timer", (t) => {
  const h = harness(t);
  h.clock.advance(10000);
  assert.equal(h.frame().hot, false);
  assert.ok(h.frame().ticks > 0 && h.frame().ticks <= STAR_MAX_TICKS);
  assert.equal(h.clock.jobs.size, 0);
  const count = h.events.length; h.clock.advance(10000);
  assert.equal(h.events.length, count);
});

test("new link force and damping settings remain bounded, backward-compatible and affect real physics", () => {
  const defaults = normalizeStarPhysics({ nodeSize: 1, edgeWidth: .85, center: .075, repel: 135, distance: 100 });
  assert.equal(defaults.linkStrength, .22); assert.equal(defaults.damping, .52);
  assert.equal(normalizeStarPhysics({ linkStrength: 99 }).linkStrength, .8);
  assert.equal(normalizeStarPhysics({ damping: -1 }).damping, .15);
  const data = createStarfieldFixture(50);
  for (const key of ['linkStrength', 'damping']) {
    const a = new StarGraphEngine(data, { [key]: STAR_PHYSICS_LIMITS[key][0] });
    const b = new StarGraphEngine(data, { [key]: STAR_PHYSICS_LIMITS[key][1] });
    try {
      a.step(20); b.step(20);
      assert.notDeepEqual(a.nodes.map(n => [n.x,n.y]), b.nodes.map(n => [n.x,n.y]), `${key} must influence physics, not only the UI`);
    } finally { a.destroy(); b.destroy(); }
  }
});

test("pause cancels even handle zero; late callbacks and hidden elapsed time cannot advance physics", (t) => {
  const h = harness(t);
  assert.equal(h.clock.first[0], 0);
  h.send({ type: "mode", mode: "paused" });
  assert.equal(h.clock.jobs.size, 0); assert.equal(h.clock.cancelled.length, 1);
  const paused = h.frame();
  h.clock.advance(10000); h.clock.cancelled[0]();
  assert.equal(h.frame(), paused); assert.equal(h.frame().hot, false);
  h.send({ type: "mode", mode: "running" });
  assert.deepEqual(h.frame().positions, paused.positions); assert.equal(h.frame().ticks, 0);
  h.clock.advance(1000 / 60); assert.equal(h.frame().ticks, 1);
});

test("pause/resume preserves the current layout and does not reheat a cold simulation", (t) => {
  const h = harness(t);
  h.clock.advance(400);
  h.send({ type: "mode", mode: "paused" });
  const paused = h.frame(); h.clock.advance(50000);
  h.send({ type: "mode", mode: "running" });
  assert.deepEqual(h.frame().positions, paused.positions); assert.equal(h.frame().ticks, paused.ticks);
  h.clock.advance(10000);
  const cold = h.frame();
  h.send({ type: "mode", mode: "paused" }); h.send({ type: "mode", mode: "running" });
  assert.equal(h.frame().hot, false); assert.equal(h.frame().ticks, cold.ticks);
  assert.deepEqual(h.frame().positions, cold.positions); assert.equal(h.clock.jobs.size, 0);
});

test("one held pin reheats a settled network, keeps the target fixed and physically pulls its neighbor", (t) => {
  const h = harness(t);
  h.clock.advance(10000);
  const before = h.frame().positions;
  const x = before[0] + 220, y = before[1] + 80;
  h.send({ type: "pin", id: "a", x, y });
  assert.equal(h.frame().hot, true); assert.equal(h.clock.jobs.size, 1);
  h.clock.advance(200);
  const held = h.frame();
  assert.equal(held.positions[0], Math.fround(x)); assert.equal(held.positions[1], Math.fround(y));
  const neighborDX = held.positions[2] - before[2], neighborDY = held.positions[3] - before[3];
  assert.ok(Math.hypot(neighborDX, neighborDY) > 1, "connected neighbor really moves while still held");
  assert.ok(neighborDX * (x - before[2]) + neighborDY * (y - before[3]) > 0, "neighbor moves toward the held point");
  h.send({ type: "release", id: "a", reheat: true });
  assert.deepEqual(h.frame().positions, held.positions, "release itself never teleports");
  h.clock.advance(200);
  assert.notEqual(h.frame().positions[0], held.positions[0]);
  h.clock.advance(10000); assert.equal(h.frame().hot, false); assert.equal(h.clock.jobs.size, 0);
});

test("frequent drag commands do not restart the timer or starve neighbor motion", (t) => {
  const h = harness(t);
  for (let i = 0; i < 100; i++) {
    h.send({ type: "pin", id: "a", x: 300 + i, y: 200 });
    assert.equal(h.clock.jobs.size, 1); h.clock.advance(1);
  }
  assert.equal(h.frame().ticks, 6); assert.equal(h.clock.cancelled.length, 0);
});

test("quiet initial layout is bounded once; pin/release/configure preserve all unpinned coordinates", (t) => {
  const h = harness(t, init(1, "quiet"));
  assert.equal(h.frame().ticks, STAR_STATIC_TICKS); assert.equal(h.frame().hot, false); assert.equal(h.clock.jobs.size, 0);
  const initial = h.frame();
  h.send({ type: "pin", id: "a", x: 444, y: 222 });
  assert.deepEqual(h.frame().positions.slice(2), initial.positions.slice(2));
  const pinned = h.frame();
  h.send({ type: "release", id: "a", reheat: true });
  h.send({ type: "mode", mode: "paused" }); h.send({ type: "mode", mode: "quiet" });
  h.send({ type: "configure", generation: 2, physics: { ...STAR_DEFAULT_PHYSICS, repel: 280 } });
  h.clock.advance(10000);
  assert.deepEqual(h.frame().positions, pinned.positions); assert.equal(h.frame().ticks, initial.ticks);
  assert.equal(h.frame().hot, false); assert.equal(h.clock.jobs.size, 0);
});

test("switching an active graph to quiet freezes it immediately without static settling", (t) => {
  const h = harness(t);
  h.clock.advance(50);
  const before = h.frame();
  h.send({ type: "mode", mode: "quiet" });
  assert.equal(h.frame().ticks, before.ticks); assert.deepEqual(h.frame().positions, before.positions);
  h.clock.advance(10000); assert.equal(h.clock.jobs.size, 0);
  h.send({ type: "mode", mode: "running" });
  assert.deepEqual(h.frame().positions, before.positions); h.clock.advance(1000 / 60);
  assert.equal(h.frame().ticks, before.ticks + 1);
});

test("paused initial graph permits direct manipulation without forced ticks or a release jump", (t) => {
  const h = harness(t, init(1, "paused"));
  const initial = h.frame();
  h.send({ type: "pin", id: "a", x: 444, y: 222 });
  h.send({ type: "release", id: "a", reheat: true });
  assert.deepEqual([...h.frame().positions.slice(0, 2)], [444, 222]);
  assert.deepEqual(h.frame().positions.slice(2), initial.positions.slice(2));
  h.clock.advance(10000); assert.equal(h.frame().ticks, 0); assert.equal(h.clock.jobs.size, 0);
});

test("reheat:false on a cold release cannot start a new cooling phase", (t) => {
  const h = harness(t);
  h.clock.advance(10000); const cold = h.frame();
  h.send({ type: "release", id: "a", reheat: false });
  assert.equal(h.frame().hot, false); assert.equal(h.frame().ticks, cold.ticks); assert.equal(h.clock.jobs.size, 0);
});

test("configuration advances generation, keeps layout, resets sequence and rejects old commands/callbacks", (t) => {
  const h = harness(t);
  h.clock.advance(50); const before = h.frame();
  h.send({ type: "configure", generation: 2, physics: { ...STAR_DEFAULT_PHYSICS, distance: 170, repel: 250 } });
  const configured = h.frame();
  assert.equal(configured.generation, 2); assert.equal(configured.sequence, 1);
  assert.deepEqual(configured.positions, before.positions); assert.equal(configured.ticks, before.ticks);
  for (const callback of h.clock.cancelled) callback();
  for (const command of [init(1), init(2), { type: "configure", physics: STAR_DEFAULT_PHYSICS, generation: 2 },
    { type: "pin", id: "a", x: 999, y: 999 }, { type: "release", id: "a", reheat: true },
    { type: "mode", mode: "paused" }, { type: "replay" }, { type: "dispose" }]) h.send(command);
  assert.equal(h.frame(), configured); assert.equal(h.clock.jobs.size, 1);
  h.clock.advance(1000 / 60);
  assert.equal(h.frame().generation, 2); assert.equal(h.frame().sequence, 2); assert.equal(h.frame().ticks, before.ticks + 1);
});

test("topology replacement discards old node order, frames and all stale interaction commands", (t) => {
  const h = harness(t);
  h.send(init(3, "paused", { ...graph, nodes: [node("c"), node("a")], edges: [] }));
  const replacement = h.frame(); assert.equal(replacement.positions.length, 4);
  assert.equal(replacement.generation, 3); assert.equal(replacement.sequence, 1); assert.equal(replacement.ticks, 0);
  h.send({ generation: 3, type: "pin", id: "c", x: 111, y: 222 });
  assert.deepEqual([...h.frame().positions.slice(0, 2)], [111, 222]);
  const pinned = h.frame();
  for (const generation of [1, 2, 4, -1, NaN, 1.5]) h.send({ generation, type: "pin", id: "a", x: 999, y: 999 });
  for (const callback of h.clock.cancelled) callback();
  assert.equal(h.frame(), pinned); assert.equal(h.clock.jobs.size, 0);
});

test("each emitted frame owns an independent transferable buffer", (t) => {
  const h = harness(t);
  const initial = h.frame(); const saved = [...initial.positions];
  const transported = structuredClone(initial, { transfer: [initial.positions.buffer] });
  assert.equal(initial.positions.byteLength, 0); assert.deepEqual([...transported.positions], saved);
  h.clock.advance(50); assert.equal(h.frame().positions.length, graph.nodes.length * 2);
  assert.notDeepEqual([...h.frame().positions], saved); assert.deepEqual([...transported.positions], saved);
  h.frame().positions.fill(1e10); h.clock.advance(50);
  assert.ok([...h.frame().positions].every((value) => Math.abs(value) <= 1100));
});

test("malformed replacement emits a generation-scoped error, cancels old work and can recover", (t) => {
  const h = harness(t);
  h.send(init(2, "running", { ...graph, nodes: null }));
  const error = h.events.at(-1);
  assert.equal(error.type, "error"); assert.equal(error.generation, 2); assert.match(error.message, /数据结构无效/);
  assert.equal(h.clock.jobs.size, 0);
  for (const callback of h.clock.cancelled) callback();
  h.send(init(1)); assert.equal(h.events.at(-1), error);
  h.send(init(3)); h.clock.advance(50);
  assert.equal(h.frame().generation, 3); assert.equal(h.frame().ticks, 3);
});

test("empty input produces a truthful empty cold frame with no timer or fabricated nodes", (t) => {
  const h = harness(t, init(1, "running", { nodes: [], edges: [], truncated: false, total_visible_nodes: 0 }));
  assert.equal(h.frame().positions.length, 0); assert.equal(h.frame().ticks, 0); assert.equal(h.frame().hot, false);
  assert.deepEqual(h.frame().bounds, { left: -200, right: 200, top: -150, bottom: 150 });
  assert.equal(h.clock.jobs.size, 0);
});

test("unknown/nonfinite pins are ignored; explicit replay is finite and clears old pins", (t) => {
  const h = harness(t, init(1, "quiet"));
  const initial = h.frame();
  h.send({ type: "pin", id: "missing", x: 0, y: 0 });
  h.send({ type: "pin", id: "a", x: NaN, y: Infinity });
  assert.equal(h.frame(), initial);
  h.send({ type: "pin", id: "a", x: 500, y: 500 }); h.send({ type: "replay" });
  assert.deepEqual(h.frame().positions, initial.positions); assert.equal(h.frame().ticks, STAR_STATIC_TICKS * 2);
  assert.equal(h.frame().hot, false); assert.equal(h.clock.jobs.size, 0);
});

test("dispose is terminal and idempotent, including already queued callbacks and newer init", (t) => {
  const h = harness(t);
  h.send({ type: "dispose" }); const count = h.events.length;
  assert.equal(h.clock.jobs.size, 0);
  h.session.dispose(); h.session.dispose();
  for (const callback of h.clock.cancelled) callback();
  h.send(init(99)); h.clock.advance(10000);
  assert.equal(h.events.length, count); assert.equal(h.clock.jobs.size, 0);
});

test("a synchronous fallback consumer can pause/dispose during emit without leaving a timer", () => {
  const clock = new ManualClock();
  let session;
  let frames = 0;
  session = createStarPhysicsSession((event) => {
    if (event.type !== "frame") return;
    frames++;
    if (event.ticks === 1) session.dispose();
  }, clock);
  session.send(init()); clock.advance(1000);
  assert.equal(frames, 2); assert.equal(clock.jobs.size, 0);
});

// Run the actual browser worker entry, adapting only self and message delivery.
function workerHarness(t) {
  const worker = new Worker(`
    const { parentPort, threadId } = require('node:worker_threads');
    globalThis.self = {
      onmessage: null,
      postMessage(event, transfers) {
        const bytes = event.type === 'frame' ? event.positions.byteLength : null;
        parentPort.postMessage(event, transfers);
        if (event.type === 'frame') parentPort.postMessage({ type: 'transfer-proof', generation: event.generation,
          sequence: event.sequence, bytes, detached: event.positions.byteLength === 0, threadId });
      }
    };
    ${workerBuild.outputFiles[0].text}
    parentPort.on('message', ({ command, request }) => {
      self.onmessage?.({ data: command });
      parentPort.postMessage({ type: 'ack', request });
    });`, { eval: true });
  const messages = [];
  const waiters = new Set();
  let failure;
  let request = 0;
  worker.on("message", (message) => {
    messages.push(message);
    for (const waiter of waiters) if (waiter.predicate(message)) { waiters.delete(waiter); waiter.resolve(message); }
  });
  worker.on("error", (error) => { failure = error; for (const waiter of waiters) waiter.reject(error); waiters.clear(); });
  const wait = (predicate) => {
    if (failure) return Promise.reject(failure);
    const found = messages.find(predicate);
    if (found) return Promise.resolve(found);
    return new Promise((resolve, reject) => {
      const waiter = { predicate, resolve: (value) => { clearTimeout(timeout); resolve(value); }, reject: (error) => { clearTimeout(timeout); reject(error); } };
      const timeout = setTimeout(() => { waiters.delete(waiter); reject(new Error("worker response timed out")); }, 5000);
      waiters.add(waiter);
    });
  };
  t.after(async () => {
    for (const waiter of waiters) waiter.reject(new Error("worker test ended"));
    waiters.clear(); await worker.terminate();
  });
  return { messages, wait, send: async (command) => {
    const id = ++request;
    worker.postMessage({ command, request: id });
    await wait((message) => message.type === "ack" && message.request === id);
  } };
}

test("real worker entry transfers a complete 300/600 layout, detaches the sender buffer and continues ticking", { timeout: 10000 }, async (t) => {
  const h = workerHarness(t);
  await h.send({ ...init(1, "quiet", createStarfieldFixture(300)), budget: "synthetic-300" });
  const first = await h.wait((event) => event.type === "frame" && event.generation === 1);
  const proof = await h.wait((event) => event.type === "transfer-proof" && event.generation === 1);
  assert.ok(first.positions instanceof Float32Array); assert.equal(first.positions.length, 600);
  assert.ok([...first.positions].every(Number.isFinite)); assert.equal(first.ticks, STAR_STATIC_TICKS);
  assert.equal(proof.bytes, 2400); assert.equal(proof.detached, true); assert.ok(proof.threadId > 0);
  await h.send(init(2));
  const moving = await h.wait((event) => event.type === "frame" && event.generation === 2 && event.ticks >= 2);
  assert.equal(moving.positions.length, 6); assert.equal(moving.hot, true);
  const movedProof = await h.wait((event) => event.type === "transfer-proof" && event.generation === 2 && event.sequence === moving.sequence);
  assert.equal(movedProof.detached, true);
  await h.send({ type: "mode", generation: 2, mode: "paused" });
  await h.send({ type: "dispose", generation: 2 });
});

for (const count of [591, 1000]) test(`real worker retains all ${count} standard nodes and ${count * 2} edges`, { timeout: 10000 }, async (t) => {
  const data = createStarfieldFixture(count);
  const model = new StarGraphEngine(data);
  try {
    assert.equal(model.nodes.length, count); assert.equal(model.links.length, count * 2);
    assert.equal(model.graph.truncated, false);
  } finally { model.destroy(); }
  const h = workerHarness(t);
  await h.send(init(1, "paused", data));
  const frame = await h.wait(event => event.type === 'frame');
  assert.equal(frame.positions.length, count * 2);
  const proof = await h.wait(event => event.type === 'transfer-proof');
  assert.equal(proof.bytes, count * 8); assert.equal(proof.detached, true);
  await h.send({ type: 'pin', generation: 1, id: `perf-${count - 1}`, x: 123, y: 456 });
  const pinned = h.messages.filter(event => event.type === 'frame').at(-1);
  assert.deepEqual([...pinned.positions.slice(-2)], [123, 456], 'last node must exist in actual Worker physics');
});

test("graph normalization does not replace the old ceiling with a 1000-node ceiling", () => {
  const nodes = Array.from({ length: 2500 }, (_, i) => node(`wide-${i}`));
  const edges = Array.from({ length: 5000 }, (_, i) => edge(`wide-edge-${i}`, `wide-${i % 2500}`, `wide-${(i + 1) % 2500}`));
  const data = { nodes, edges, truncated: false, total_visible_nodes: 2500 };
  assert.equal(prepareStarGraph(data).nodes.length, 2500);
  assert.equal(prepareStarGraph(data).edges.length, 5000);
  assert.equal(prepareStarGraph(data).truncated, false);
});

test("real worker orders errors, replacement generations, stale commands and quiet release accurately", { timeout: 10000 }, async (t) => {
  const h = workerHarness(t);
  await h.send(init(1, "running", null));
  const error = await h.wait((event) => event.type === "error");
  assert.equal(error.generation, 1); assert.match(error.message, /数据结构无效/);
  await h.send(init(2, "quiet"));
  await h.send({ type: "pin", generation: 2, id: "b", x: 321, y: 123 });
  const pinned = h.messages.filter((event) => event.type === "frame").at(-1);
  assert.deepEqual([...pinned.positions.slice(2, 4)], [321, 123]);
  await h.send({ type: "release", generation: 2, id: "b", reheat: true });
  assert.deepEqual(h.messages.filter((event) => event.type === "frame").at(-1).positions, pinned.positions);
  await h.send({ type: "configure", generation: 3, physics: { ...STAR_DEFAULT_PHYSICS, distance: 150 } });
  const configured = h.messages.filter((event) => event.type === "frame").at(-1);
  assert.equal(configured.sequence, 1); assert.equal(configured.generation, 3);
  const count = h.messages.filter((event) => event.type === "frame").length;
  await h.send({ type: "pin", generation: 2, id: "b", x: 999, y: 999 });
  await h.send({ type: "dispose", generation: 2 });
  assert.equal(h.messages.filter((event) => event.type === "frame").length, count);
  await h.send({ type: "pin", generation: 3, id: "a", x: 11, y: 22 });
  assert.deepEqual([...h.messages.filter((event) => event.type === "frame").at(-1).positions.slice(0, 2)], [11, 22]);
  await h.send({ type: "dispose", generation: 3 });
  const disposedCount = h.messages.filter((event) => event.type === "frame").length;
  await h.send(init(4));
  assert.equal(h.messages.filter((event) => event.type === "frame").length, disposedCount);
});
