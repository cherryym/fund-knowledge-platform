// v8 behavioral regressions. Real JSDOM, GSAP, Runtime and PhysicsClient; manual
// display RAF and controlled Worker positions. Record actual painter.draw calls,
// never idle RAF counts. This is not GPU/presentation-FPS or browser evidence.
// Run: node --test src/star-canvas-runtime.test.mjs
import assert from 'node:assert/strict';
import test, { after, afterEach } from 'node:test';
import Module, { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const require = createRequire(import.meta.url), sourceDir = fileURLToPath(new URL('.', import.meta.url));
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/', pretendToBeVisual: true });
const { window } = dom;
for (const name of ['window', 'document', 'Element', 'HTMLElement', 'HTMLCanvasElement', 'SVGElement', 'Node', 'Event', 'MouseEvent']) {
  globalThis[name] = name === 'window' ? window : window[name];
}
globalThis.getComputedStyle = window.getComputedStyle.bind(window);
let visible = true, focused = true;
document.hasFocus = () => focused;
Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visible ? 'visible' : 'hidden' });
window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
const errors = [];
window.addEventListener('error', event => { errors.push(event.error ?? new Error(event.message)); event.preventDefault(); });
const gsap = require('gsap').gsap;
gsap.ticker.sleep();

class DisplayClock {
  now = 1000;
  next = 1;
  frames = new Map();
  timers = new Map();
  cancelled = new Set();
  performance = { now: () => this.now };
  dateNow = () => 1800000000000 + this.now;
  raf = callback => { const id = this.next++; this.frames.set(id, callback); return id; };
  cancel = id => { this.cancelled.add(id); this.frames.delete(id); };
  timeout = (callback, delay = 0) => {
    const id = this.next++;
    this.timers.set(id, { callback, due: this.now + Math.max(0, Number(delay) || 0), interval: 0 }); return id;
  };
  clear = id => this.timers.delete(id);
  interval = (callback, delay) => { const id = this.timeout(callback, delay); this.timers.get(id).interval = Math.max(1, Number(delay) || 1); return id; };
  step(dt = 1000 / 60) {
    this.now += dt;
    for (let turn = 0; ; turn++) {
      const due = [...this.timers].filter(([, timer]) => timer.due <= this.now + 1e-7).sort((a, b) => a[1].due - b[1].due)[0];
      if (!due) break;
      assert.ok(turn < 1000, 'bounded timer work per display frame');
      const [id, timer] = due;
      if (timer.interval) timer.due += timer.interval; else this.timers.delete(id);
      timer.callback();
    }
    gsap.updateRoot(this.now / 1000); gsap.ticker.sleep();
    const callbacks = [...this.frames]; this.frames.clear();
    for (const [, callback] of callbacks) callback(this.now);
  }
  advance(count, dt = 1000 / 60) { for (let i = 0; i < count; i++) this.step(dt); }
}
const clock = new DisplayClock();
// d3's stopped-simulation housekeeping also chooses window RAF. Route it to
// this clock so cleanup is deterministic, rather than mixing real/fake time.
window.requestAnimationFrame = clock.raf;
window.cancelAnimationFrame = clock.cancel;
const output = await build({
  stdin: { contents: 'export * from "./starCanvasRuntime"; export * from "./starPhysicsClient"; export * from "./starGraphTheme"; export * from "./visualInteraction"; export * from "./starfieldFixture"; export { createStarPainter } from "./starPainter"; export { createStarPhysicsSession } from "./starGraphPhysics";', resolveDir: sourceDir, loader: 'ts' },
  bundle: true, platform: 'node', format: 'cjs', write: false, logLevel: 'silent', external: ['gsap'],
  define: {
    requestAnimationFrame: '__displayClock.raf', cancelAnimationFrame: '__displayClock.cancel',
    setTimeout: '__displayClock.timeout', clearTimeout: '__displayClock.clear',
    'globalThis.setTimeout': '__displayClock.timeout', 'globalThis.clearTimeout': '__displayClock.clear',
    setInterval: '__displayClock.interval', clearInterval: '__displayClock.clear',
    performance: '__displayClock.performance', 'Date.now': '__displayClock.dateNow',
  },
});
const bundle = new Module(`${sourceDir}__canvas_test_memory.cjs`);
bundle.filename = `${sourceDir}__canvas_test_memory.cjs`; bundle.paths = Module._nodeModulePaths(sourceDir);
const runtimeRequire = bundle.require.bind(bundle);
bundle.require = name => name === 'gsap' ? gsap : runtimeRequire(name);
bundle._compile(`module.exports = (__displayClock) => { ${output.outputFiles[0].text}\nreturn module.exports; };`, bundle.filename);
const star = bundle.exports(clock), fixtures = new Set();

class TestObserver {
  static instances = new Set();
  disconnected = false;
  constructor(callback) { this.callback = callback; TestObserver.instances.add(this); }
  observe(target) { this.target = target; }
  disconnect() { this.disconnected = true; }
  emit(value = true) { this.callback([{ target: this.target, isIntersecting: value, intersectionRatio: value ? 1 : 0 }]); }
}
globalThis.ResizeObserver = TestObserver;
function freeze(value) {
  if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value); }
  return value;
}
function near(actual, expected, tolerance = 1e-5, message = '') {
  assert.ok(Math.abs(actual - expected) <= tolerance, `${message}: ${actual} != ${expected} (tolerance ${tolerance})`);
}
function xy(node) { return { x: node.x, y: node.y }; }
function canvasContext(canvas, calls) {
  return { canvas, setTransform() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {},
    stroke() { calls.push({ type: 'stroke' }); },
    arc(x, y, radius) { calls.push({ type: 'arc', x, y, radius }); }, fill() {},
    createRadialGradient() { return { addColorStop() {} }; }, measureText(text) { return { width: text.length * 7 }; },
    fillText(text, x, y) { calls.push({ type: 'label', text, x, y }); },
  };
}
function scene({ count = 300, paused = false, quiet = false, selection = null,
  rect: initialRect = { left: 10, top: 20, width: 960, height: 620 }, realPainter = false, realPhysics = false } = {}) {
  const graph = freeze(star.createStarfieldFixture(count)), before = structuredClone(graph);
  const canvas = document.createElement('canvas'), labels = document.createElement('canvas');
  document.body.append(canvas, labels);
  let rect = { ...initialRect };
  canvas.getBoundingClientRect = () => ({ ...rect, x: rect.left, y: rect.top, right: rect.left + rect.width, bottom: rect.top + rect.height, toJSON() {} });
  const captures = new Set();
  canvas.setPointerCapture = id => captures.add(id);
  canvas.hasPointerCapture = id => captures.has(id);
  canvas.releasePointerCapture = id => captures.delete(id);
  const opened = [], selected = [], phases = [], backends = [], draws = [], commands = [], resizes = [], contextCalls = [];
  const theme = star.defaultStarTheme(); theme.labels = 'none'; theme.nodes[graph.nodes[0].id] = '#225588';
  const state = { mode: 'webgl', submit: true, disposed: 0, invalidation: null, physicsDisposed: 0 };
  const worker = { terminated: 0, onmessage: null, onerror: null, onmessageerror: null,
    postMessage(command) { commands.push(structuredClone(command)); worker.session?.send(command); },
    terminate() { this.terminated++; this.session?.dispose(); },
  };
  if (realPhysics) worker.session = star.createStarPhysicsSession(event => worker.onmessage?.({ data: event }), {
    now: () => clock.now, setTimeout: clock.timeout, clearTimeout: clock.clear,
  });
  let real;
  if (realPainter) {
    const context = canvasContext(canvas, contextCalls), text = canvasContext(labels, contextCalls);
    canvas.getContext = type => type === '2d' ? context : null;
    labels.getContext = type => type === '2d' ? text : null;
  }
  const runtime = star.mountStarCanvas({ canvas, labels, graph, theme, paused, quiet, selection,
    renderBudget: count === 300 ? 'synthetic-300' : 'standard', level: quiet ? 'quiet' : 'rich', diagnostics: true,
    onOpen: node => opened.push(node.id), onSelect: id => selected.push(id), onPhase: value => phases.push(value), onBackend: value => backends.push(value),
  }, {
    painterFactory(surface, overlay, invalidate) {
      state.invalidation = invalidate;
      if (realPainter) real = star.createStarPainter(surface, overlay, invalidate);
      return {
        get mode() { return real?.mode ?? state.mode; },
        resize(...args) { resizes.push(args); real?.resize(...args); },
        draw(frame) {
          assert.equal(state.disposed, 0, 'Runtime cannot submit after painter disposal');
          const submitted = real ? real.draw(frame) : state.submit;
          draws.push({ time: clock.now, submitted, ...structuredClone(frame) }); return submitted;
        },
        dispose() { state.disposed++; real?.dispose(); },
      };
    },
    physicsFactory(options) {
      const client = star.createStarPhysicsClient({ ...options, workerFactory: () => worker });
      return { send: command => client.send(command), dispose() { state.physicsDisposed++; client.dispose(); } };
    },
  });
  gsap.ticker.sleep();
  let sequence = 0;
  const positions = Float32Array.from(graph.nodes.flatMap((_, i) => i === 0 ? [0, 0] : [((i - 1) % 20 - 9.5) * 36, (Math.floor((i - 1) / 20) - 7) * 30]));
  const fixture = { runtime, canvas, labels, graph, before, theme, captures, opened, selected, phases, backends,
    draws, commands, resizes, state, positions, worker, contextCalls,
    last: () => { assert.ok(draws.length, 'test requires an actual draw call'); return draws.at(-1); },
    emit({ hot = false, ticks = 0, bounds = { left: -360, right: 360, top: -240, bottom: 240 }, coordinates = positions } = {}) {
      worker.onmessage?.({ data: { type: 'frame', generation: commands.at(-1).generation, sequence: ++sequence,
        positions: coordinates.slice(), bounds, ticks, hot } });
    },
    clientPoint(worldPoint, camera = fixture.last().camera) {
      const scale = Math.min(rect.width / 960, rect.height / 620);
      return { x: rect.left + (rect.width - 960 * scale) / 2 + (camera.x + worldPoint.x * camera.k) * scale,
        y: rect.top + (rect.height - 620 * scale) / 2 + (camera.y + worldPoint.y * camera.k) * scale };
    },
    pointer(type, point, id = 1, extra = {}) {
      const event = new window.MouseEvent(type, { bubbles: true, cancelable: true, button: 0, clientX: point.x, clientY: point.y, ...extra });
      Object.defineProperty(event, 'pointerId', { value: id }); canvas.dispatchEvent(event); return event;
    },
    click(point, detail = 1) { canvas.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true, clientX: point.x, clientY: point.y, detail })); },
    wheel(deltaY, point = { x: rect.left + rect.width * .38, y: rect.top + rect.height * .44 }, deltaMode = 0) {
      const event = new window.WheelEvent('wheel', { bubbles: true, cancelable: true, clientX: point.x, clientY: point.y, deltaY, deltaMode });
      canvas.dispatchEvent(event); return event;
    },
    resize(next) { rect = { ...rect, ...next }; [...TestObserver.instances].find(observer => observer.target === canvas && !observer.disconnected)?.emit(); },
    dispose() { runtime.dispose(); canvas.remove(); labels.remove(); fixtures.delete(fixture); },
  };
  fixtures.add(fixture);
  if (!realPhysics) fixture.emit();
  clock.advance(45);
  return fixture;
}

afterEach(() => {
  const active = [...fixtures];
  for (const fixture of active) fixture.dispose();
  clock.advance(8); gsap.ticker.sleep();
  const observers = [...TestObserver.instances], eventErrors = errors.splice(0);
  TestObserver.instances.clear();
  visible = focused = true; delete globalThis.IntersectionObserver;
  for (const fixture of active) assert.deepEqual(fixture.graph, fixture.before, 'all interactions must preserve API data');
  assert.equal(star.isGraphInteracting(), false, 'no leaked graph interaction owner');
  assert.equal(clock.frames.size, 0, 'no leaked display RAF');
  assert.equal(clock.timers.size, 0, 'no leaked client timeout / physics timer');
  assert.equal(document.querySelectorAll('canvas').length, 0);
  assert.ok(observers.every(observer => observer.disconnected), 'all observers must disconnect');
  assert.deepEqual(eventErrors, [], 'JSDOM must not swallow Runtime event exceptions');
});
after(() => { gsap.ticker.sleep(); gsap.globalTimeline.clear(); dom.window.close(); });

test('300-node / 600-edge input, ordering and endpoints are preserved through draw and interactions', () => {
  const c = scene();
  const check = () => {
    const drawn = c.last();
    assert.equal(drawn.nodes.length, 300); assert.equal(drawn.edges.length, 600);
    assert.deepEqual(drawn.nodes.map(node => node.id), c.graph.nodes.map(node => node.id));
    drawn.edges.forEach((edge, i) => {
      assert.equal(drawn.nodes[edge.source].id, c.graph.edges[i].source);
      assert.equal(drawn.nodes[edge.target].id, c.graph.edges[i].target);
    });
  };
  check(); c.wheel(-30); clock.advance(35); c.runtime.select(c.graph.nodes[10].id); clock.advance(30); check();
  assert.equal(c.canvas.dataset.starDrawNodes, '300'); assert.equal(c.canvas.dataset.starDrawEdges, '600');
  assert.deepEqual(c.commands[0].graph, c.before);
});

for (const hz of [60, 120]) test(`${hz}Hz held and released neighbors interpolate in every actual draw between 30Hz Worker frames`, () => {
  const c = scene(), dt = 1000 / hz, every = hz / 30;
  const original = c.last(), camera = { ...original.camera };
  const start = c.clientPoint(original.nodes[0]);
  const end = { x: start.x + 80, y: start.y - 45 };
  c.pointer('pointerdown', start); c.pointer('pointermove', end);
  const expectedPin = { x: original.nodes[0].x + 80 / camera.k, y: original.nodes[0].y - 45 / camera.k };
  let previous = xy(c.last().nodes[1]);
  let heldPaints = 0, releasePaints = 0, messageCount = 0;
  function frames(released) {
    for (let i = 0; i < hz / 2; i++) {
      if (i % every === 0) {
        c.positions[2] += 12; c.positions[3] += 6;
        c.positions[0] = released ? c.positions[0] + 8 : expectedPin.x;
        c.positions[1] = released ? c.positions[1] + 4 : expectedPin.y;
        c.emit({ hot: true, ticks: ++messageCount });
      }
      const target = { x: c.positions[2], y: c.positions[3] }, before = c.draws.length;
      clock.step(dt);
      assert.equal(c.draws.length, before + 1, `${released ? 'released' : 'held'} display frame ${i} must call draw`);
      const drawn = c.last(), node = drawn.nodes[1], blend = 1 - Math.exp(-dt / 28);
      near(node.x, previous.x + (target.x - previous.x) * blend, 1e-5, 'real neighbor x interpolation');
      near(node.y, previous.y + (target.y - previous.y) * blend, 1e-5, 'real neighbor y interpolation');
      assert.ok(node.x > previous.x && node.x < target.x, 'must show intermediate geometry, not repeat a Worker snapshot');
      assert.deepEqual(drawn.camera, camera, 'held/released node motion must not reset the camera');
      if (!released) { near(drawn.nodes[0].x, expectedPin.x); near(drawn.nodes[0].y, expectedPin.y); heldPaints++; }
      else releasePaints++;
      previous = xy(node);
    }
  }
  frames(false);
  assert.ok(c.commands.filter(command => command.type === 'mode').every(command => command.mode === 'running'), 'holding a node cannot pause neighbor physics');
  c.pointer('pointerup', end); c.click(end);
  assert.equal(star.isGraphInteracting(), false, 'release immediately hands control back to cooling');
  frames(true);
  assert.equal(heldPaints, hz / 2); assert.equal(releasePaints, hz / 2);
  assert.equal(messageCount, 30, '30 controlled Worker snapshots must produce 60/120 independent geometry submissions');
  assert.deepEqual(c.opened, []); assert.equal(c.captures.size, 0);
});

test('direct drag follows continuous pointer input at 120Hz with no accidental open or camera movement', () => {
  const c = scene(), first = c.last(), start = c.clientPoint(first.nodes[0]);
  c.pointer('pointerdown', start);
  for (let i = 1; i <= 60; i++) {
    const point = { x: start.x + i, y: start.y - i * .5 };
    c.pointer('pointermove', point); clock.step(1000 / 120);
    if (i >= 5) {
      near(c.last().nodes[0].x, first.nodes[0].x + i / first.camera.k);
      near(c.last().nodes[0].y, first.nodes[0].y - i * .5 / first.camera.k);
    }
    assert.deepEqual(c.last().camera, first.camera);
  }
  const end = { x: start.x + 60, y: start.y - 30 };
  c.pointer('pointerup', end); c.click(end); clock.step();
  assert.deepEqual(c.opened, []); assert.equal(c.captures.size, 0);
});

function assertUnfocused(c) {
  const frame = c.last();
  assert.ok(frame.nodes.every(node => node.opacity === 1), 'all nodes return to ordinary opacity');
  assert.ok(frame.edges.every(edge => edge.opacity === .58 && edge.color === c.theme.edge), 'all edges return to ordinary user color and opacity');
  assert.equal(c.canvas.dataset.starSelected, ''); assert.equal(c.canvas.dataset.starDimCount, '0');
}
for (const options of [{}, { paused: true }, { quiet: true }]) test(`release clears selection and stationary capture/position events cannot restore hover ${JSON.stringify(options)}`, () => {
  const c = scene({ ...options, selection: 'perf-0' }), camera = { ...c.last().camera };
  const start = c.clientPoint(c.last().nodes[0]), end = { x: start.x + 75, y: start.y - 45 };
  c.pointer('pointerdown', start); c.pointer('pointermove', end); clock.advance(10);
  assert.ok(c.last().nodes.some(node => node.opacity < .5), 'held node has temporary focus');
  const held = xy(c.last().nodes[0]);
  c.positions[0] = held.x; c.positions[1] = held.y;
  c.pointer('pointerup', end); c.click(end);
  assert.deepEqual(c.selected, [null], 'external/list selection clears synchronously once');
  clock.step(); assert.equal(c.canvas.dataset.starSelected, '');
  c.pointer('lostpointercapture', end); c.pointer('pointerover', end);
  c.pointer('pointermove', end); c.pointer('pointerleave', end); c.pointer('pointerover', end);
  c.positions[2] = held.x; c.positions[3] = held.y; c.emit({ hot: !options.paused && !options.quiet, ticks: 1 });
  clock.advance(45); assertUnfocused(c);
  assert.deepEqual(c.last().camera, camera); assert.deepEqual(c.opened, []);
  // Deliberate movement >4 CSS px restores hit testing using displayed positions.
  c.pointer('pointermove', c.clientPoint(c.last().nodes[10])); clock.advance(30);
  assert.ok(c.last().nodes.some(node => node.opacity < .5), 'intentional movement re-enables hover');
});

test('captured clicks open once without pin/release, and a deliberate click still works after drag suppression', () => {
  const c = scene();
  const clickNode = () => {
    const point = c.clientPoint(c.last().nodes[0]);
    c.pointer('pointerdown', point); c.pointer('pointerup', point); c.click(point); clock.step();
  };
  const before = c.commands.length;
  for (let i = 0; i < 10; i++) clickNode();
  assert.deepEqual(c.opened, Array(10).fill('perf-0')); assert.deepEqual(c.selected, ['perf-0']);
  assert.equal(c.commands.slice(before).filter(command => ['pin', 'release', 'replay'].includes(command.type)).length, 0);
  const start = c.clientPoint(c.last().nodes[0]), end = { x: start.x + 60, y: start.y + 50 };
  c.pointer('pointerdown', start); c.pointer('pointermove', end); clock.step();
  c.pointer('pointerup', end); c.click(end); clock.step();
  assert.equal(c.opened.length, 10);
  clickNode(); assert.equal(c.opened.length, 11); assert.equal(c.opened.at(-1), 'perf-0');
  assert.equal(c.captures.size, 0);
});

for (const options of [{ paused: true }, { quiet: true }]) test(`real session allows zoom/drag in ${JSON.stringify(options)} without neighbor ticks or release jumps`, () => {
  const c = scene({ ...options, count: 50, realPhysics: true }), first = c.last();
  const positions = first.nodes.map(xy), ticks = c.canvas.dataset.starTicks;
  c.wheel(-90); clock.advance(50);
  assert.ok(c.last().camera.k > first.camera.k, 'static preference retains direct zoom');
  assert.deepEqual(c.last().nodes.map(xy), positions, 'zoom cannot force physics movement');
  const camera = { ...c.last().camera }, start = c.clientPoint(c.last().nodes[0]);
  const end = { x: start.x + 50, y: start.y + 30 };
  c.pointer('pointerdown', start); c.pointer('pointermove', end); clock.advance(30);
  const held = c.last().nodes.map(xy);
  assert.notDeepEqual(held[0], positions[0]); assert.deepEqual(held.slice(1), positions.slice(1));
  c.pointer('pointerup', end); c.click(end); clock.advance(60);
  // Direct input is double precision; the protocol explicitly transfers F32.
  // Require exactly that rounding for the released point, with unchanged peers.
  const transferred = held.map(point => ({ x: Math.fround(point.x), y: Math.fround(point.y) }));
  assert.deepEqual(c.last().nodes.map(xy), transferred, 'quiet/paused release cannot settle or reseed beyond F32 transport rounding');
  assert.equal(c.canvas.dataset.starTicks, ticks); assert.deepEqual(c.last().camera, camera);
  assert.deepEqual(c.opened, []);
});

test('quiet zoom commits in one draw and schedules no further camera submissions', () => {
  const c = scene({ quiet: true }), before = c.last().camera;
  for (let i = 0; i < 8; i++) c.wheel(-3);
  const start = c.draws.length; clock.step(1000 / 120);
  assert.equal(c.draws.length, start + 1); assert.ok(c.last().camera.k > before.k);
  const drawn = c.last(), count = c.draws.length;
  clock.advance(90); assert.equal(c.draws.length, count); assert.deepEqual(c.last(), drawn);
});

test('switching to quiet during zoom commits a stable camera and cancels future inertia', () => {
  const c = scene(); c.wheel(-100); clock.step(1000 / 120);
  c.runtime.activity(false, true, 'quiet'); clock.step(1000 / 120);
  const camera = c.last().camera, count = c.draws.length;
  clock.advance(90); assert.deepEqual(c.last().camera, camera); assert.equal(c.draws.length, count);
});

for (const hz of [60, 120]) test(`${hz}Hz continuous wheel draws the anchored camera in a letterboxed canvas`, () => {
  const c = scene({ rect: { left: 83, top: 47, width: 1200, height: 620 } });
  const initial = c.last().camera, world = { x: 55, y: -35 }, anchor = c.clientPoint(world, initial);
  let previous = initial;
  for (let i = 0; i < hz / 2; i++) {
    assert.equal(c.wheel(-2, anchor).defaultPrevented, true);
    const before = c.draws.length; clock.step(1000 / hz);
    assert.equal(c.draws.length, before + 1, `wheel frame ${i} must submit camera geometry`);
    const current = c.last().camera, projected = c.clientPoint(world, current);
    near(projected.x, anchor.x, 1e-5, 'cursor world anchor x'); near(projected.y, anchor.y, 1e-5, 'cursor world anchor y');
    assert.ok(current.k > previous.k, 'wheel must change the drawn camera each screen frame');
    previous = current;
  }
  clock.advance(90);
  near(c.last().camera.k, initial.k * Math.exp((hz / 2) * 2 * .0025));
  assert.equal(star.isGraphInteracting(), false);
  const count = c.draws.length; clock.advance(30); assert.equal(c.draws.length, count, 'settled camera must stop drawing');
});

test('pressing during zoom anchors drag to the last drawn camera without a first-frame jump', () => {
  const c = scene(); c.wheel(-160); clock.step(1000 / 120);
  const drawn = c.last(), start = c.clientPoint(drawn.nodes[0]);
  c.pointer('pointerdown', start); clock.step(1000 / 120);
  assert.deepEqual(c.last().camera, drawn.camera); assert.deepEqual(xy(c.last().nodes[0]), xy(drawn.nodes[0]));
  const end = { x: start.x + 45, y: start.y + 35 };
  c.pointer('pointermove', end); clock.step(1000 / 120);
  near(c.last().nodes[0].x, drawn.nodes[0].x + 45 / drawn.camera.k);
  near(c.last().nodes[0].y, drawn.nodes[0].y + 35 / drawn.camera.k);
  assert.deepEqual(c.last().camera, drawn.camera);
  c.pointer('pointerup', end); c.click(end); assert.deepEqual(c.opened, []);
});

test('background pan uses CSS/world scale and preserves camera through resize after user navigation', () => {
  const c = scene({ rect: { left: 41, top: 73, width: 700, height: 500 } });
  const camera = c.last().camera, start = c.clientPoint({ x: -460, y: -280 });
  c.pointer('pointerdown', start); c.pointer('pointermove', { x: start.x + 42, y: start.y + 28 }); clock.step();
  near(c.last().camera.x, camera.x + 42 / (700 / 960));
  near(c.last().camera.y, camera.y + 28 / (700 / 960)); near(c.last().camera.k, camera.k);
  assert.equal(c.commands.filter(command => command.type === 'pin').length, 0);
  c.pointer('pointerup', { x: start.x + 42, y: start.y + 28 });
  const panned = c.last().camera; c.resize({ width: 1000, height: 620 }); clock.step();
  assert.deepEqual(c.last().camera, panned); assert.deepEqual(c.resizes.at(-1).slice(0, 2), [1000, 620]);
});

for (const event of ['pointercancel', 'lostpointercapture']) test(`${event} clears capture/selection and cannot open a cancelled drag`, () => {
  const c = scene({ selection: 'perf-0' }), start = c.clientPoint(c.last().nodes[0]), end = { x: start.x + 60, y: start.y + 35 };
  c.pointer('pointerdown', start); c.pointer('pointermove', end); clock.step();
  c.pointer(event, end); c.click(end); clock.advance(45);
  assert.equal(c.captures.size, 0); assert.equal(c.canvas.classList.contains('is-dragging'), false);
  assert.equal(star.isGraphInteracting(), false); assert.deepEqual(c.selected, [null]); assert.deepEqual(c.opened, []);
  assert.equal(c.commands.filter(command => command.type === 'release').at(-1).reheat, false);
  assertUnfocused(c);
});

for (const reason of ['hidden', 'blur', 'offscreen']) test(`${reason} cancels captured drag and RAF, suppresses stale callbacks, and resumes without replay`, () => {
  let intersection;
  if (reason === 'offscreen') {
    intersection = class extends TestObserver {};
    globalThis.IntersectionObserver = intersection;
  }
  const c = scene({ selection: 'perf-0' });
  const observer = intersection && [...TestObserver.instances].find(value => value instanceof intersection);
  if (observer) { observer.emit(true); clock.advance(45); }
  const start = c.clientPoint(c.last().nodes[0]), end = { x: start.x + 60, y: start.y + 35 };
  c.pointer('pointerdown', start); c.pointer('pointermove', end);
  const pending = [...clock.frames]; assert.ok(pending.length);
  if (reason === 'hidden') { visible = false; document.dispatchEvent(new window.Event('visibilitychange')); }
  if (reason === 'blur') { focused = false; window.dispatchEvent(new window.Event('blur')); }
  if (observer) observer.emit(false);
  assert.equal(c.captures.size, 0); assert.equal(star.isGraphInteracting(), false); assert.equal(clock.frames.size, 0);
  assert.ok(pending.every(([id]) => clock.cancelled.has(id)), 'remove scheduled display callbacks');
  assert.equal(c.commands.at(-1).mode, 'paused');
  const count = c.draws.length;
  for (const [, callback] of pending) callback(clock.now + 16);
  c.positions[2] += 20; c.emit({ hot: true }); c.pointer('pointerup', end); c.click(end);
  clock.advance(15); assert.equal(c.draws.length, count); assert.deepEqual(c.opened, []);
  if (reason === 'hidden') { visible = true; document.dispatchEvent(new window.Event('visibilitychange')); }
  if (reason === 'blur') { focused = true; window.dispatchEvent(new window.Event('focus')); }
  if (observer) observer.emit(true);
  clock.advance(45);
  assert.ok(c.draws.length > count); assert.equal(c.commands.filter(command => command.type === 'replay').length, 0);
  assert.equal(c.canvas.classList.contains('is-dragging'), false); assertUnfocused(c);
});

test('dispose clears capture, listeners, owned GSAP/RAF, observers, painter and Client exactly once', () => {
  const c = scene(), start = c.clientPoint(c.last().nodes[0]), end = { x: start.x + 70, y: start.y - 35 };
  c.pointer('pointerdown', start); c.pointer('pointermove', end);
  const pending = [...clock.frames], lateFrame = c.worker.onmessage, invalidate = c.state.invalidation;
  assert.equal(c.captures.size, 1);
  c.runtime.dispose(); c.runtime.dispose();
  const count = c.draws.length, commands = c.commands.length, selected = c.selected.length;
  assert.equal(c.captures.size, 0); assert.equal(c.canvas.classList.contains('is-dragging'), false);
  assert.equal(c.state.disposed, 1); assert.equal(c.state.physicsDisposed, 1); assert.equal(c.worker.terminated, 1);
  assert.ok(pending.every(([id]) => clock.cancelled.has(id)));
  for (const [, callback] of pending) callback(clock.now + 16);
  lateFrame({ data: { type: 'frame', generation: c.commands[0].generation, sequence: 999,
    positions: c.positions.slice(), hot: true, ticks: 900, bounds: { left: -360, right: 360, top: -240, bottom: 240 } } });
  invalidate();
  for (const observer of TestObserver.instances) observer.emit();
  c.pointer('pointerup', end); c.pointer('pointerdown', start); c.click(start, 0); c.wheel(-100);
  document.dispatchEvent(new window.Event('visibilitychange')); window.dispatchEvent(new window.Event('focus'));
  c.runtime.zoom(2); c.runtime.fit(); c.runtime.replay(); c.runtime.select('perf-1'); c.runtime.activity(false, false, 'rich');
  c.runtime.theme({ ...c.theme, edge: '#123456' }); clock.advance(90);
  assert.equal(c.draws.length, count); assert.equal(c.commands.length, commands); assert.equal(c.selected.length, selected);
  assert.deepEqual(c.opened, []); assert.equal(star.isGraphInteracting(), false);
  assert.equal(gsap.getTweensOf([c.canvas, c.labels]).length, 0, 'owned GSAP entrance must be killed');
});

test('disposing one of two Runtime owners does not release the other active graph', () => {
  const a = scene(), b = scene(), states = [];
  const unsubscribe = star.subscribeGraphInteraction(value => states.push(value));
  try {
    for (let i = 0; i < 30; i++) a.wheel(-1);
    b.wheel(-20); assert.deepEqual(states, [true]);
    a.dispose(); assert.equal(star.isGraphInteracting(), true);
    b.dispose(); assert.deepEqual(states, [true, false]);
  } finally { unsubscribe(); }
});

test('failed painter submission is not counted, and context invalidation retries the actual frame', () => {
  const c = scene(), count = Number(c.canvas.dataset.starFrames);
  c.state.submit = false; c.state.invalidation(); clock.step();
  assert.equal(c.last().submitted, false); assert.equal(Number(c.canvas.dataset.starFrames), count);
  c.state.submit = true; c.state.invalidation(); clock.step();
  assert.equal(c.last().submitted, true); assert.equal(Number(c.canvas.dataset.starFrames), count + 1);
  assert.equal(c.last().nodes.length, 300);
});

test('context fallback/restoration updates Runtime backend reporting when painter mode changes', () => {
  const c = scene(); assert.equal(c.canvas.dataset.starRenderer, 'webgl');
  c.state.mode = 'canvas2d'; c.state.invalidation(); clock.step();
  assert.equal(c.last().submitted, true);
  assert.equal(c.canvas.dataset.starRenderer, 'canvas2d', 'Runtime must report a changed painter mode after context fallback');
  assert.match(c.backends.at(-1), /Canvas 2D/);
  c.state.mode = 'webgl'; c.state.invalidation(); clock.step();
  assert.equal(c.canvas.dataset.starRenderer, 'webgl'); assert.match(c.backends.at(-1), /WebGL/);
});

test('real painter falls back from unavailable WebGL and receives matching Canvas2D geometry', () => {
  const c = scene({ realPainter: true });
  assert.equal(c.canvas.dataset.starRenderer, 'canvas2d'); assert.match(c.backends.at(-1), /Canvas 2D/);
  c.contextCalls.length = 0; c.state.invalidation(); clock.step();
  const drawn = c.last(), node = drawn.nodes[0], expected = c.clientPoint(node, drawn.camera);
  const arc = c.contextCalls.find(call => call.type === 'arc' && Math.abs(call.radius - node.radius * drawn.camera.k) < 1e-5
    && Math.abs(call.x - (expected.x - 10)) < 1e-5 && Math.abs(call.y - (expected.y - 20)) < 1e-5);
  assert.ok(arc, 'production 2D painter must receive the Runtime node coordinates and camera');
  assert.equal(c.contextCalls.filter(call => call.type === 'stroke').length, 600);
  assert.equal(drawn.submitted, true);
  const count = c.draws.length;
  const lost = new window.Event('webglcontextlost', { cancelable: true }); c.canvas.dispatchEvent(lost); clock.step();
  assert.equal(lost.defaultPrevented, true); assert.ok(c.draws.length > count); assert.equal(c.last().submitted, true);
  c.canvas.dispatchEvent(new window.Event('webglcontextrestored')); clock.step(); assert.equal(c.last().submitted, true);
});
