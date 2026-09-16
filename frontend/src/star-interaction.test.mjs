// v5 behavioral counterexamples. Real engine/d3/GSAP + private SVG DOM; only
// screen-frame/timer delivery, geometry and pointer capture are deterministic.
// A passing test proves actual SVG writes, not real-browser FPS or model quality.
// Run ONLY after the main agent confirms the v5 engine interface is ready.
import assert from 'node:assert/strict';
import test, { after, afterEach } from 'node:test';
import Module, { createRequire } from 'node:module';
import { resolve } from 'node:path';
import { build } from 'esbuild';

const require = createRequire(import.meta.url);
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/', pretendToBeVisual: true });
const { window } = dom;
for (const name of ['window', 'document', 'Element', 'HTMLElement', 'SVGElement', 'Node', 'Event', 'MouseEvent']) {
  globalThis[name] = name === 'window' ? window : window[name];
}
globalThis.getComputedStyle = window.getComputedStyle.bind(window);
document.hasFocus = () => true;
Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
const gsap = require('gsap').gsap;
const physicsClocks = new Set();
const originalTickerAdd = gsap.ticker.add, originalTickerRemove = gsap.ticker.remove;
gsap.ticker.add = function (callback, ...args) {
  const result = originalTickerAdd.call(this, callback, ...args); physicsClocks.add(callback); return result;
};
gsap.ticker.remove = function (callback) { physicsClocks.delete(callback); return originalTickerRemove.call(this, callback); };

class ScreenClock {
  now = 0;
  next = 1;
  frames = new Map();
  timers = new Map();
  cancelled = new Set();
  performance = { now: () => this.now };
  dateNow = () => 1800000000000 + this.now;
  raf = callback => { const id = this.next++; this.frames.set(id, callback); return id; };
  cancel = id => { this.cancelled.add(id); this.frames.delete(id); };
  timeout = (callback, delay = 0) => {
    const id = this.next++; this.timers.set(id, { callback, due: this.now + Math.max(0, Number(delay) || 0), repeat: 0 }); return id;
  };
  clear = id => { this.timers.delete(id); };
  interval = (callback, delay = 0) => {
    const id = this.timeout(callback, delay); this.timers.get(id).repeat = Math.max(1, Number(delay) || 1); return id;
  };
  step(milliseconds = 1000 / 60) {
    this.now += milliseconds;
    for (let turn = 0; turn < 1000; turn++) {
      const due = [...this.timers].filter(([, value]) => value.due <= this.now).sort((a, b) => a[1].due - b[1].due);
      if (!due.length) break;
      for (const [id, value] of due) {
        if (!this.timers.has(id)) continue;
        if (value.repeat) value.due += value.repeat; else this.timers.delete(id);
        value.callback();
      }
      assert.ok(turn < 999, 'unbounded timer work in one screen frame');
    }
    const frame = [...this.frames]; this.frames.clear();
    for (const [, callback] of frame) callback(this.now);
  }
  advance(count, interval = 1000 / 60) { for (let index = 0; index < count; index++) this.step(interval); }
}
const clock = new ScreenClock();
globalThis.__STAR_INTERACTION_CLOCK__ = clock;
window.requestAnimationFrame = clock.raf;
window.cancelAnimationFrame = clock.cancel;
// Clock substitutions are lexical to this isolated engine bundle, not changes to
// production source, GSAP methods, Node's test runner timers or process clocks.
const output = await build({
  stdin: { contents: 'export * from "./starGraphEngine"; export * from "./starGraphTheme"; export * from "./visualInteraction";',
    resolveDir: resolve('src'), loader: 'ts' },
  bundle: true, platform: 'node', format: 'cjs', write: false, logLevel: 'silent',
  external: ['gsap'],
  banner: { js: 'const __starClock = globalThis.__STAR_INTERACTION_CLOCK__;' },
  define: {
    requestAnimationFrame: '__starClock.raf', cancelAnimationFrame: '__starClock.cancel',
    setTimeout: '__starClock.timeout', clearTimeout: '__starClock.clear',
    setInterval: '__starClock.interval', clearInterval: '__starClock.clear',
    performance: '__starClock.performance', 'Date.now': '__starClock.dateNow',
  },
});
const module = new Module(resolve('src/__star_interaction_runtime.cjs'));
module.filename = resolve('src/__star_interaction_runtime.cjs');
module.paths = Module._nodeModulePaths(resolve('src'));
const runtimeRequire = module.require.bind(module);
module.require = id => id === 'gsap' ? gsap : runtimeRequire(id);
module._compile(output.outputFiles[0].text, module.filename);
const star = module.exports;
const SVG = 'http://www.w3.org/2000/svg';
const fixtures = new Set();
const errors = [];
window.addEventListener('error', event => errors.push(event.error ?? new Error(event.message)));
const STATIC_ATTRIBUTES = new Set(['fill', 'r', 'stroke', 'stroke-width', 'font-size', 'text-anchor', 'x']);

function graphData(count = 200) {
  const nodes = Array.from({ length: count }, (_, i) => ({ id: `n${i}`, label: `Synthetic ${i}`,
    kind: 'knowledge', knowledge_type: 'rule', category: 'synthetic', state: 'APPROVED', version_id: null }));
  const edges = Array.from({ length: count * 2 }, (_, i) => ({ id: `e${i}`, source: `n${i % count}`,
    target: `n${(i % count + (i < count ? 1 : 7)) % count}`, type: 'WIKI_LINK', origin: 'wikilink', state: 'ACTIVE' }));
  return { nodes, edges, truncated: false, total_visible_nodes: count };
}
function camera(element) {
  const transform = element.getAttribute('transform') || '';
  const match = transform.match(/translate\(\s*([-+\d.e]+)[,\s]+([-+\d.e]+)\s*\)\s*scale\(\s*([-+\d.e]+)\s*\)/);
  assert.ok(match, `actual viewport camera transform required, got ${transform}`);
  const result = { x: Number(match[1]), y: Number(match[2]), k: Number(match[3]) };
  assert.ok(Object.values(result).every(Number.isFinite));
  return result;
}
function world(point, view) { return { x: (point.x - view.x) / view.k, y: (point.y - view.y) / view.k }; }
function near(actual, expected, tolerance = .12) { assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} != ${expected}`); }
function nodePosition(element) {
  const match = (element.getAttribute('transform') || '').match(/translate\(\s*([-+\d.e]+)[,\s]+([-+\d.e]+)\s*\)/);
  assert.ok(match, 'node position must actually be drawn');
  return { x: Number(match[1]), y: Number(match[2]) };
}

function scene({ quiet = false, paused = true, nodeCount = 200, rect = { left: 10, top: 20, width: 960, height: 620 } } = {}) {
  const writes = [], opened = [], selected = [], phases = [];
  const captures = new Set();
  const doc = window.document;
  const create = (tag, kind, id = '') => {
    const element = doc.createElementNS(SVG, tag);
    const set = element.setAttribute.bind(element);
    element.setAttribute = (name, value) => { writes.push({ kind, id, name, value: String(value), frame: clock.now }); set(name, value); };
    for (const property of ['opacity', 'display', 'fontSize']) {
      const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element.style), property);
      if (descriptor?.set) Object.defineProperty(element.style, property, { configurable: true,
        get: () => descriptor.get.call(element.style), set: value => {
          writes.push({ kind, id, name: property, value: String(value), frame: clock.now }); descriptor.set.call(element.style, value);
        } });
    }
    if (tag === 'text') {
      const descriptor = Object.getOwnPropertyDescriptor(window.Node.prototype, 'textContent');
      Object.defineProperty(element, 'textContent', { configurable: true,
        get: () => descriptor.get.call(element), set: value => {
          writes.push({ kind, id, name: 'textContent', value: String(value), frame: clock.now }); descriptor.set.call(element, value);
        } });
    }
    return element;
  };
  const surface = create('svg', 'surface'); surface.setAttribute('viewBox', '0 0 960 620');
  surface.getBoundingClientRect = () => ({ ...rect, x: rect.left, y: rect.top, right: rect.left + rect.width,
    bottom: rect.top + rect.height, toJSON() {} });
  surface.setPointerCapture = id => captures.add(id);
  surface.hasPointerCapture = id => captures.has(id);
  surface.releasePointerCapture = id => captures.delete(id);
  const viewport = create('g', 'viewport');
  const edgeLayer = create('g', 'edge-layer'), starLayer = create('g', 'star-layer');
  viewport.append(edgeLayer, starLayer); surface.append(viewport); doc.body.append(surface);
  const data = graphData(nodeCount), frozenInput = structuredClone(data), nodes = new Map(), edges = new Map();
  for (const item of data.nodes) {
    const position = create('g', 'position', item.id); position.dataset.starNode = item.id;
    const glyph = create('g', 'glyph', item.id), dot = create('circle', 'dot', item.id), text = create('text', 'label', item.id);
    glyph.append(dot); position.append(glyph, text); starLayer.append(position);
    nodes.set(item.id, { position, glyph, dot, text });
  }
  for (const item of data.edges) { const element = create('path', 'edge', item.id); edges.set(item.id, element); edgeLayer.append(element); }
  let runtime;
  const context = gsap.context(() => {
    runtime = star.mountStarGraph({ surface, viewport, edgeLayer, starLayer, nodes, edges, graph: data,
      renderBudget: nodeCount === 300 ? 'synthetic-300' : 'standard',
      theme: star.defaultStarTheme(), paused, quiet, level: quiet ? 'quiet' : 'rich', selection: null,
      contextSafe: callback => callback, onOpen: node => opened.push(node.id),
      onSelect: id => selected.push(id), onPhase: phase => phases.push(phase) });
  }, surface);
  clock.advance(4);
  const svgPoint = (clientX, clientY) => {
    const scale = Math.min(rect.width / 960, rect.height / 620);
    return { x: (clientX - rect.left - (rect.width - 960 * scale) / 2) / scale,
      y: (clientY - rect.top - (rect.height - 620 * scale) / 2) / scale };
  };
  const wheel = (deltaY, { deltaMode = 0, x = rect.left + rect.width * .4, y = rect.top + rect.height * .45 } = {}) => {
    const event = new window.WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY, deltaMode, clientX: x, clientY: y });
    surface.dispatchEvent(event); assert.equal(event.defaultPrevented, true); return svgPoint(x, y);
  };
  const pointer = (type, target, x, y, id = 1) => {
    const event = new window.MouseEvent(type, { bubbles: true, cancelable: true, button: 0, clientX: x, clientY: y });
    Object.defineProperty(event, 'pointerId', { value: id }); target.dispatchEvent(event); return event;
  };
  const click = (target, detail = 0) => target.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true, detail }));
  const result = { runtime, surface, viewport, data, frozenInput, nodes, edges, writes, opened, selected, phases, captures,
    wheel, pointer, click, svgPoint, clear: () => { writes.length = 0; }, camera: () => camera(viewport),
    viewportWrites: () => writes.filter(write => write.kind === 'viewport' && write.name === 'transform'),
    staticWrites: () => writes.filter(write => ['dot', 'label', 'edge'].includes(write.kind)
      && (STATIC_ATTRIBUTES.has(write.name) || write.name === 'fontSize' || write.name === 'textContent')),
    dispose: () => { runtime.dispose(); context.revert(); surface.remove(); fixtures.delete(result); } };
  fixtures.add(result);
  result.clear();
  return result;
}

afterEach(() => {
  for (const fixture of [...fixtures]) {
    assert.deepEqual(fixture.data, fixture.frozenInput, 'interaction must not mutate API graph data');
    fixture.dispose();
  }
  clock.advance(4);
  assert.equal(document.querySelectorAll('svg').length, 0);
  assert.equal(physicsClocks.size, 0, 'the graph must remove only its owned physics listener');
  assert.deepEqual(errors.splice(0), []);
});
after(() => { gsap.ticker.sleep(); gsap.ticker.add = originalTickerAdd; gsap.ticker.remove = originalTickerRemove;
  dom.window.close(); delete globalThis.__STAR_INTERACTION_CLOCK__; });

test('wheel burst updates one camera target without repainting 200 dots and 400 edges per event', () => {
  const chart = scene();
  const before = chart.viewport.getAttribute('transform');
  const existing = new Set(clock.frames.keys());
  for (let index = 0; index < 60; index++) chart.wheel(-1);
  assert.equal(chart.viewport.getAttribute('transform'), before, 'input handlers should not paint immediately');
  assert.equal(chart.writes.filter(write => ['position', 'glyph', 'dot', 'label', 'edge'].includes(write.kind)).length, 0,
    '60 wheel events must not repeatedly write all scene geometry/style');
  assert.equal([...clock.frames.keys()].filter(id => !existing.has(id)).length, 1, 'one pending screen-frame callback per burst');
  clock.step();
  assert.equal(chart.viewportWrites().length, 1);
  assert.equal(chart.staticWrites().length, 0, 'fill/r/font/stroke remain cached during the gesture');
  assert.equal(chart.nodes.size, 200); assert.equal(chart.edges.size, 400);
});

for (const hz of [60, 120]) test(`camera paints distinct eased intermediate SVG frames at ${hz}Hz, independent of 30/8Hz physics`, () => {
  const chart = scene();
  const initial = chart.camera();
  chart.wheel(-96);
  const frames = [];
  // Six 60Hz frames stay within the declared 120ms label quiet-window.
  for (let index = 0; index < 6; index++) {
    chart.clear(); clock.step(1000 / hz);
    frames.push(chart.camera());
    assert.equal(chart.viewportWrites().length, 1, 'a callback without a viewport write is not a painted frame');
    assert.equal(chart.staticWrites().length, 0);
  }
  assert.ok(frames[0].k > initial.k);
  assert.ok(frames.every((frame, index) => index === 0 || frame.k > frames[index - 1].k), 'camera must progress on successive screen frames');
  clock.advance(120, 1000 / hz);
  const final = chart.camera();
  assert.ok(final.k > frames[0].k && final.k >= frames.at(-1).k, 'single-frame camera jumps fail this test');
});

test('wheel deltaMode pixels, lines and pages normalize to the same physical delta', () => {
  // Contract normalization: line=16 CSS px; page=surface client height.
  const samples = [[-32, 0], [-2, 1], [-32 / 620, 2]];
  const scales = [];
  for (const [delta, mode] of samples) {
    const chart = scene({ quiet: true });
    const before = chart.camera(); chart.wheel(delta, { deltaMode: mode }); clock.advance(2);
    scales.push(chart.camera().k / before.k); chart.dispose();
  }
  near(scales[0], scales[1], .0003); near(scales[0], scales[2], .0003);
  assert.ok(scales[0] > 1);
});

test('wheel clamp is applied per normalized event at 240px instead of depending on deltaMode', () => {
  const scales = [];
  for (const delta of [-240, -2400]) {
    const chart = scene({ quiet: true }); const before = chart.camera();
    chart.wheel(delta); clock.advance(2); scales.push(chart.camera().k / before.k); chart.dispose();
  }
  near(scales[0], scales[1], .0003);
});

test('every rendered zoom frame preserves the pointer anchor with non-square client letterboxing', () => {
  const chart = scene({ rect: { left: 30, top: 40, width: 480, height: 400 } });
  const point = chart.svgPoint(180, 210), anchor = world(point, chart.camera());
  chart.wheel(-70, { x: 180, y: 210 });
  for (let index = 0; index < 30; index++) {
    clock.step(1000 / 120);
    const drawn = chart.camera();
    near(drawn.x + anchor.x * drawn.k, point.x);
    near(drawn.y + anchor.y * drawn.k, point.y);
  }
});

test('reverse wheel input turns the rendered camera without discontinuity or stale inertia', () => {
  const chart = scene();
  chart.wheel(-60); clock.advance(4, 1000 / 120);
  const before = chart.camera(); chart.clear();
  chart.wheel(120);
  assert.deepEqual(chart.camera(), before, 'reverse input still coalesces before paint');
  const seen = [];
  for (let index = 0; index < 20; index++) { clock.step(1000 / 120); seen.push(chart.camera().k); }
  assert.ok(seen.some(scale => scale < before.k), 'reverse input must eventually zoom out');
  assert.ok(Math.abs(seen[0] - before.k) < before.k * .2, 'no full target jump at reversal');
});

test('background pointermove bursts paint the last pointer position once per frame while paused', () => {
  const chart = scene({ paused: true });
  const before = chart.camera(); const ticks = chart.surface.dataset.starTicks;
  chart.pointer('pointerdown', chart.surface, 210, 220); chart.clear();
  for (let index = 1; index <= 30; index++) chart.pointer('pointermove', chart.surface, 210 + index, 220 + index / 2);
  assert.equal(chart.viewportWrites().length, 0); assert.equal(chart.staticWrites().length, 0);
  clock.step();
  assert.equal(chart.viewportWrites().length, 1);
  const drawn = chart.camera(); near(drawn.x - before.x, 30, .02); near(drawn.y - before.y, 15, .02);
  assert.equal(chart.surface.dataset.starTicks, ticks, 'pause stops physics, not direct manipulation');
  chart.pointer('pointerup', chart.surface, 240, 235);
  chart.click(chart.surface, 1);
  assert.equal(chart.captures.size, 0); assert.deepEqual(chart.opened, []);
});

test('node drag bursts coalesce geometry writes and commit the final move before pointerup', () => {
  const chart = scene({ paused: true });
  const target = chart.nodes.get('n0').dot;
  const before = chart.nodes.get('n0').position.getAttribute('transform');
  const originalPoint = nodePosition(chart.nodes.get('n0').position), drawnCamera = chart.camera();
  chart.pointer('pointerdown', target, 490, 330); chart.clear();
  for (let index = 1; index <= 30; index++) chart.pointer('pointermove', chart.surface, 490 + index, 330 + index);
  assert.equal(chart.writes.filter(write => write.kind === 'position' || write.name === 'd').length, 0);
  clock.step();
  const geometry = chart.writes.filter(write => write.kind === 'position' && write.name === 'transform' || write.name === 'd');
  assert.ok(geometry.length > 0 && geometry.length <= 600, 'at most one geometry pass, not thirty full repaints');
  assert.notEqual(chart.nodes.get('n0').position.getAttribute('transform'), before);
  chart.pointer('pointermove', chart.surface, 550, 390);
  chart.pointer('pointerup', chart.surface, 550, 390);
  clock.advance(2);
  const finalPoint = nodePosition(chart.nodes.get('n0').position);
  near(finalPoint.x - originalPoint.x, 60 / drawnCamera.k);
  near(finalPoint.y - originalPoint.y, 60 / drawnCamera.k);
  chart.click(chart.surface, 1);
  assert.deepEqual(chart.opened, []); assert.equal(chart.captures.size, 0);
});

test('300-node live drag pulls neighbors before release, follows pointer, and caps solver to 60Hz', () => {
  const chart = scene({ nodeCount: 300, paused: false });
  for (let i = 0; i < 260; i++) {
    clock.step(34); for (const callback of [...physicsClocks]) callback(clock.now / 1000);
  }
  assert.equal(chart.surface.dataset.starPhase, 'settled');
  const initialCamera = chart.camera();
  const initialPoint = nodePosition(chart.nodes.get('n250').position);
  const beforeNeighbors = ['n243', 'n249', 'n251', 'n257'].map(id => chart.nodes.get(id).position.getAttribute('transform'));
  const ticks = Number(chart.surface.dataset.starTicks);
  const x = 10 + initialCamera.x + initialPoint.x * initialCamera.k;
  const y = 20 + initialCamera.y + initialPoint.y * initialCamera.k;
  chart.pointer('pointerdown', chart.nodes.get('n250').dot, x, y);
  for (let i = 1; i <= 120; i++) {
    chart.pointer('pointermove', chart.surface, x + i * .8, y - i * .4);
    clock.step(1000 / 120);
    // Even if the GSAP clock also fires, it must not double-step the drag solver.
    for (const callback of [...physicsClocks]) callback(clock.now / 1000);
  }
  const held = nodePosition(chart.nodes.get('n250').position);
  near(held.x - initialPoint.x, 96 / initialCamera.k);
  near(held.y - initialPoint.y, -48 / initialCamera.k);
  assert.deepEqual(chart.camera(), initialCamera, 'node drag must not move the viewport');
  const afterNeighbors = ['n243', 'n249', 'n251', 'n257'].map(id => chart.nodes.get(id).position.getAttribute('transform'));
  assert.ok(afterNeighbors.every((value, index) => value !== beforeNeighbors[index]), 'all direct neighbors must respond while still held');
  const addedTicks = Number(chart.surface.dataset.starTicks) - ticks;
  assert.ok(addedTicks > 30 && addedTicks <= 60, `bounded active solver, got ${addedTicks} ticks`);
  assert.deepEqual(chart.opened, []);
  chart.pointer('pointerup', chart.surface, x + 96, y - 48);
  assert.equal(star.isGraphInteracting(), false, 'release must not freeze cooling for the idle timeout');
  const releaseTicks = Number(chart.surface.dataset.starTicks);
  clock.step(34); for (const callback of [...physicsClocks]) callback(clock.now / 1000);
  assert.ok(Number(chart.surface.dataset.starTicks) > releaseTicks, 'cooling resumes on the next physics frame');
  chart.click(chart.surface, 1); assert.deepEqual(chart.opened, []);
});

test('held drag continues settling its neighbors when pointer input stops, then stops bounded work', () => {
  const chart = scene({ nodeCount: 300, paused: false });
  chart.pointer('pointerdown', chart.nodes.get('n0').dot, 490, 330);
  chart.pointer('pointermove', chart.surface, 570, 290); clock.step();
  const before = chart.nodes.get('n1').position.getAttribute('transform');
  const anchor = chart.nodes.get('n0').position.getAttribute('transform');
  clock.advance(20);
  assert.notEqual(chart.nodes.get('n1').position.getAttribute('transform'), before);
  assert.equal(chart.nodes.get('n0').position.getAttribute('transform'), anchor, 'held star stays pinned');
  clock.advance(260);
  const ticks = chart.surface.dataset.starTicks;
  clock.advance(100);
  assert.equal(chart.surface.dataset.starTicks, ticks, 'a stationary hold cannot cause endless force work');
});

for (const options of [{ paused: true }, { paused: false, quiet: true }]) test(`static preferences preserve direct node dragging without involuntary neighbor motion ${JSON.stringify(options)}`, () => {
  const chart = scene({ ...options, nodeCount: 300 });
  const neighbor = chart.nodes.get('n1').position.getAttribute('transform');
  const target = chart.nodes.get('n0').position.getAttribute('transform');
  const ticks = chart.surface.dataset.starTicks;
  chart.pointer('pointerdown', chart.nodes.get('n0').dot, 490, 330);
  chart.pointer('pointermove', chart.surface, 540, 300); clock.advance(40);
  assert.notEqual(chart.nodes.get('n0').position.getAttribute('transform'), target);
  assert.equal(chart.nodes.get('n1').position.getAttribute('transform'), neighbor);
  assert.equal(chart.surface.dataset.starTicks, ticks);
});

test('cancelling an active force drag stops its private frame and never opens a node', () => {
  const chart = scene({ nodeCount: 300, paused: false });
  chart.pointer('pointerdown', chart.nodes.get('n0').dot, 490, 330);
  chart.pointer('pointermove', chart.surface, 570, 300); clock.step();
  const pending = [...clock.frames];
  chart.pointer('pointercancel', chart.surface, 570, 300);
  assert.equal(chart.captures.size, 0); assert.equal(star.isGraphInteracting(), false);
  chart.dispose(); const writes = chart.writes.length;
  for (const [, callback] of pending) callback(clock.now + 16);
  clock.advance(30); assert.equal(chart.writes.length, writes); assert.deepEqual(chart.opened, []);
});

function assertUnfocused(chart) {
  assert.ok([...chart.nodes.values()].every(({ position }) => position.style.opacity === '1' && !position.classList.contains('is-selected')));
  assert.ok([...chart.edges.values()].every(edge => edge.style.opacity === '0.58' && edge.getAttribute('stroke') === star.defaultStarTheme().edge));
}

for (const options of [{ paused: false }, { paused: true }, { paused: false, quiet: true }]) test(`drag release restores the complete 300-node view even when previously selected ${JSON.stringify(options)}`, () => {
  const chart = scene({ nodeCount: 300, ...options });
  chart.runtime.select('n16'); clock.advance(2);
  assert.ok(chart.nodes.get('n16').position.classList.contains('is-selected'));
  const initialCamera = chart.camera();
  chart.pointer('pointerdown', chart.nodes.get('n16').dot, 490, 330);
  chart.pointer('pointermove', chart.surface, 550, 370); clock.advance(2);
  assert.ok([...chart.nodes.values()].some(({ position }) => position.style.opacity === '0.23'), 'drag still has temporary focus');
  const held = chart.nodes.get('n16').position.getAttribute('transform');
  chart.pointer('pointerup', chart.surface, 550, 370); chart.click(chart.surface, 1);
  assertUnfocused(chart);
  assert.equal(chart.nodes.get('n16').position.getAttribute('transform'), held, 'release clears focus, not layout');
  assert.deepEqual(chart.camera(), initialCamera, 'release must not reset pan or zoom');
  assert.deepEqual(chart.selected, [null]); assert.deepEqual(chart.opened, []);
  // Capture release and force motion can both produce boundary events even
  // while the physical mouse has not moved. None may re-dim the graph.
  chart.pointer('pointerover', chart.nodes.get('n16').dot, 550, 370);
  chart.pointer('pointermove', chart.nodes.get('n16').dot, 550, 370);
  chart.pointer('pointerleave', chart.surface, 550, 370);
  chart.pointer('pointerover', chart.nodes.get('n17').dot, 550, 370);
  clock.advance(20); assertUnfocused(chart);
  chart.pointer('pointermove', chart.nodes.get('n17').dot, 570, 380);
  clock.advance(2);
  assert.ok([...chart.nodes.values()].some(({ position }) => position.style.opacity === '0.23'), 'deliberate movement restores normal hover');
});

test('drag-release hover suppression never blocks a deliberate click or keyboard selection', () => {
  const chart = scene();
  const dragNode = () => {
    chart.pointer('pointerdown', chart.nodes.get('n0').dot, 490, 330);
    chart.pointer('pointermove', chart.surface, 550, 370); clock.step();
    chart.pointer('pointerup', chart.surface, 550, 370); chart.click(chart.surface, 1);
    assertUnfocused(chart);
  };
  dragNode();
  chart.pointer('pointerdown', chart.nodes.get('n0').dot, 550, 370);
  chart.pointer('pointerup', chart.surface, 550, 370); chart.click(chart.surface, 1);
  assert.deepEqual(chart.opened, ['n0']); assert.ok(chart.nodes.get('n0').position.classList.contains('is-selected'));
  dragNode(); chart.click(chart.nodes.get('n1').dot, 0);
  assert.deepEqual(chart.opened, ['n0', 'n1']); assert.ok(chart.nodes.get('n1').position.classList.contains('is-selected'));
});

test('repeated external selection and pointer down/up do not duplicate writes or onSelect callbacks', () => {
  const chart = scene();
  chart.runtime.select('n0'); clock.advance(2); chart.clear();
  for (let index = 0; index < 30; index++) chart.runtime.select('n0');
  clock.advance(2);
  assert.equal(chart.writes.length, 0, 'same selection is a no-op, including unchanged style assignments');
  const target = chart.nodes.get('n1').dot;
  chart.pointer('pointerdown', target, 490, 330);
  chart.pointer('pointerup', chart.surface, 490, 330);
  chart.click(chart.surface, 1); clock.advance(2);
  assert.deepEqual(chart.selected, ['n1']); assert.deepEqual(chart.opened, ['n1']);
});

test('ten captured clicks select/open correctly without pinning, reheating or adding physics ticks', () => {
  const chart = scene({ paused: false });
  const tick = () => { clock.step(34); for (const callback of [...physicsClocks]) callback(clock.now / 1000, 34, 1); };
  for (let index = 0; index < 260; index++) tick();
  assert.equal(chart.surface.dataset.starPhase, 'settled');
  const settledTicks = Number(chart.surface.dataset.starTicks);
  const actions = [];
  const prototype = star.StarGraphEngine.prototype;
  const originals = Object.fromEntries(['pin', 'release', 'reheat'].map(name => [name, prototype[name]]));
  for (const name of Object.keys(originals)) prototype[name] = function (...args) {
    actions.push(name); return originals[name].apply(this, args);
  };
  try {
    for (let index = 0; index < 10; index++) {
      chart.pointer('pointerdown', chart.nodes.get(`n${index}`).dot, 490, 330);
      chart.pointer('pointerup', chart.surface, 490, 330);
      chart.click(chart.surface, 1); tick();
    }
    for (let index = 0; index < 30; index++) tick();
    assert.equal(Number(chart.surface.dataset.starTicks), settledTicks, 'selection must not restart a cooled simulation');
    assert.deepEqual(actions, [], 'click is not a drag: do not pin/release/reheat');
    assert.deepEqual(chart.opened, Array.from({ length: 10 }, (_, index) => `n${index}`));
    assert.deepEqual(chart.selected, chart.opened);
  } finally { for (const [name, fn] of Object.entries(originals)) prototype[name] = fn; }
});

test('quiet applies the accumulated camera in one frame and has no post-input inertial paints', () => {
  const chart = scene({ quiet: true });
  const before = chart.camera();
  for (let index = 0; index < 8; index++) chart.wheel(-3);
  clock.step(); assert.ok(chart.camera().k > before.k);
  const final = chart.camera(); chart.clear(); clock.advance(60);
  assert.deepEqual(chart.camera(), final);
  assert.equal(chart.viewportWrites().length, 0);
});

test('switching to quiet during eased zoom commits a stable camera and stops future inertia', () => {
  const chart = scene(); chart.wheel(-100); clock.step(1000 / 120);
  chart.runtime.activity(true, true, 'quiet'); clock.step();
  const final = chart.camera(); chart.clear(); clock.advance(90);
  assert.deepEqual(chart.camera(), final); assert.equal(chart.viewportWrites().length, 0);
});

for (const eventName of ['pointercancel', 'lostpointercapture']) test(`${eventName} releases capture and cancels pending drag paint without opening a node`, () => {
  const chart = scene();
  const target = chart.nodes.get('n0').dot;
  chart.pointer('pointerdown', target, 490, 330); chart.clear();
  chart.pointer('pointermove', chart.surface, 530, 370);
  const pending = [...clock.frames.keys()];
  assert.ok(pending.length > 0);
  chart.pointer(eventName, chart.surface, 530, 370);
  assert.ok(pending.some(id => clock.cancelled.has(id)), 'cancel must remove a pending frame, not merely leave a no-op queued');
  assert.equal(chart.surface.dataset.starInteracting, 'false');
  assert.equal(star.isGraphInteracting(), false);
  chart.click(chart.surface, 1); chart.clear(); clock.advance(10);
  assert.equal(chart.captures.size, 0); assert.deepEqual(chart.opened, []);
  assert.equal(chart.writes.filter(write => ['position', 'viewport'].includes(write.kind) && write.name === 'transform').length, 0);
});

test('dispose cancels its scheduled camera frame and stale callbacks cannot repaint detached DOM', () => {
  const chart = scene();
  chart.wheel(-80);
  const pending = [...clock.frames]; assert.ok(pending.length > 0);
  chart.dispose(); const afterDispose = chart.writes.length;
  assert.ok(pending.some(([id]) => clock.cancelled.has(id)));
  for (const [, callback] of pending) callback(clock.now + 16);
  clock.advance(90);
  assert.equal(chart.writes.length, afterDispose);
  assert.deepEqual(chart.opened, []); assert.equal(chart.captures.size, 0);
});

test('anonymous interaction ownership is deduplicated across bursts and independently released on disposal', () => {
  const first = scene(), second = scene();
  const states = [];
  const unsubscribe = star.subscribeGraphInteraction(active => states.push(active));
  try {
    for (let i = 0; i < 30; i++) first.wheel(-1);
    second.wheel(-10);
    assert.equal(first.surface.dataset.starInteracting, 'true');
    assert.equal(second.surface.dataset.starInteracting, 'true');
    assert.deepEqual(states, [true]);
    first.dispose();
    assert.equal(star.isGraphInteracting(), true, 'one disposer cannot release another graph owner');
    assert.deepEqual(states, [true]);
    second.dispose();
    assert.equal(star.isGraphInteracting(), false);
    assert.deepEqual(states, [true, false]);
  } finally { unsubscribe(); }
});

test('completed wheel gestures release coordination only after the final camera is drawn', () => {
  const first = scene(), second = scene();
  const states = [];
  const unsubscribe = star.subscribeGraphInteraction(active => states.push(active));
  try {
    first.wheel(-80); clock.advance(6);
    second.wheel(-80);
    assert.deepEqual(states, [true]);
    clock.advance(120);
    assert.equal(star.isGraphInteracting(), false, 'completed gestures must not leave ambient paused');
    assert.deepEqual(states, [true, false], 'only aggregate start/end transitions are delivered');
    for (const chart of [first, second]) {
      assert.equal(chart.surface.dataset.starInteracting, 'false');
      assert.equal(chart.surface.classList.contains('is-navigating'), false);
      const final = chart.camera(); chart.clear(); clock.advance(30);
      assert.deepEqual(chart.camera(), final);
      assert.equal(chart.viewportWrites().length, 0, 'camera must already be committed when ownership ends');
    }
  } finally { unsubscribe(); }
});

test('disposing a captured node drag releases capture and drag UI without notifying or repainting later', () => {
  const chart = scene();
  chart.pointer('pointerdown', chart.nodes.get('n0').dot, 490, 330);
  chart.pointer('pointermove', chart.surface, 530, 370);
  assert.equal(chart.captures.size, 1);
  assert.equal(chart.surface.classList.contains('is-dragging'), true);
  assert.equal(star.isGraphInteracting(), true);
  const pending = [...clock.frames];
  // Inspect the live surface before React removes it; runtime cleanup must not
  // depend on detach implicitly clearing browser pointer capture.
  chart.runtime.dispose();
  assert.equal(chart.captures.size, 0, 'dispose must release its captured pointer');
  assert.equal(chart.surface.classList.contains('is-dragging'), false);
  assert.equal(chart.surface.classList.contains('is-navigating'), false);
  assert.equal(star.isGraphInteracting(), false);
  chart.clear();
  for (const [, callback] of pending) callback(clock.now + 16);
  chart.pointer('pointerup', chart.surface, 530, 370);
  clock.advance(90);
  assert.equal(chart.writes.length, 0);
  assert.deepEqual(chart.opened, []); assert.deepEqual(chart.selected, []);
  chart.runtime.dispose();
  assert.equal(chart.writes.length, 0, 'dispose is idempotent');
});
