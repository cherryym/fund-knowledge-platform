// Real d3-force + GSAP in a private JSDOM. Geometry/visibility events are explicit
// DOM stand-ins, never a claim of real browser frame rate or pointer retargeting.
import test, { after, afterEach } from "node:test";
import assert from "node:assert/strict";
import Module, { createRequire } from "node:module";
import { resolve } from "node:path";
import { readFileSync } from "node:fs";
import { build } from "esbuild";
const require = createRequire(import.meta.url);
const { JSDOM } = require("jsdom");
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { url: "http://localhost/", pretendToBeVisual: true });
const { window } = dom;
for (const name of ["window", "document", "Element", "HTMLElement", "SVGElement", "Node", "Event", "MouseEvent", "MutationObserver"])
  globalThis[name] = name === "window" ? window : window[name];
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
globalThis.requestAnimationFrame = window.requestAnimationFrame.bind(window);
globalThis.cancelAnimationFrame = window.cancelAnimationFrame.bind(window);
const nativeComputed = window.getComputedStyle.bind(window);
globalThis.getComputedStyle = window.getComputedStyle = (element) => new Proxy(nativeComputed(element), {
  get(target, property) {
    const value = target[property];
    if (["translate", "scale", "rotate"].includes(property) && value === "") return "none";
    return typeof value === "function" ? value.bind(target) : value;
  },
});
let visibility = "visible";
let focused = true;
Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
document.hasFocus = () => focused;
const media = new Map();
window.matchMedia = (query) => {
  if (!media.has(query)) {
    const value = new window.EventTarget();
    Object.assign(value, { media: query, matches: false,
      addListener(fn) { this.addEventListener("change", fn); }, removeListener(fn) { this.removeEventListener("change", fn); } });
    media.set(query, value);
  }
  return media.get(query);
};
const intersections = new Set();
const resizes = new Set();
globalThis.ResizeObserver = class {
  constructor(callback) { this.callback = callback; resizes.add(this); }
  observe(target) { this.target = target; }
  disconnect() { resizes.delete(this); }
  emit() { this.callback([{ target: this.target }]); }
};
globalThis.IntersectionObserver = class {
  constructor(callback) { this.callback = callback; intersections.add(this); }
  observe(target) { this.target = target; }
  unobserve() {}
  disconnect() { intersections.delete(this); }
  emit(visible) { this.callback([{ target: this.target, isIntersecting: visible, intersectionRatio: visible ? 1 : 0 }]); }
};
const captures = new Set();
window.SVGElement.prototype.setPointerCapture = (id) => captures.add(id);
window.SVGElement.prototype.hasPointerCapture = (id) => captures.has(id);
window.SVGElement.prototype.releasePointerCapture = (id) => captures.delete(id);
const React = require("react");
const { act } = React;
const { createRoot } = require("react-dom/client");
const gsap = require("gsap").gsap;
const { ScrollTrigger } = require("gsap/dist/ScrollTrigger");
const clocks = new Set();
const originalAdd = gsap.ticker.add;
const originalRemove = gsap.ticker.remove;
gsap.ticker.add = function (listener, ...args) {
  // GSAP add() first calls remove() to deduplicate. Observe after that internal
  // removal, so the spy reflects the real registered clock rather than zero.
  const value = originalAdd.call(this, listener, ...args); clocks.add(listener); return value;
};
gsap.ticker.remove = function (listener) { clocks.delete(listener); return originalRemove.call(this, listener); };
const result = await build({
  stdin: { contents: `export * from './src/KnowledgeGraph'; export * from './src/starGraphEngine'; export * from './src/starGraphTheme'; export * from './src/starfieldFixture'; export { boundedGraph } from './src/knowledgeTypes';`,
    resolveDir: process.cwd(), loader: "tsx" },
  bundle: true, write: false, platform: "node", format: "cjs", loader: { ".css": "empty" },
  external: ["react", "react/*", "react-dom", "react-dom/*", "gsap", "gsap/ScrollTrigger", "@gsap/react"],
});
const filename = resolve("src/__star_graph_test_bundle.cjs");
const compiled = new Module(filename);
compiled.filename = filename; compiled.paths = Module._nodeModulePaths(resolve("src"));
const runtimeRequire = compiled.require.bind(compiled);
// Share the actual GSAP/ScrollTrigger instances with the app shell; no UI or
// animation implementation substitutes. This only adapts Node default interop.
compiled.require = (id) => id === "gsap" ? gsap : id === "gsap/ScrollTrigger" ? { ScrollTrigger } : runtimeRequire(id);
compiled._compile(result.outputFiles[0].text, filename);
const star = compiled.exports;
let root;
const errors = [];
window.addEventListener("error", (event) => errors.push(event.error));
const node = (id, overrides = {}) => ({ id, label: `真实测试节点 ${id}`, kind: "knowledge", knowledge_type: "rule", category: "测试", state: "APPROVED", version_id: null, ...overrides });
const edge = (id, source, target) => ({ id, source, target, type: "WIKI_LINK", origin: "wikilink", state: "ACTIVE" });
const graph = { nodes: [node("a"), node("b", { kind: "document" }), node("c", { knowledge_type: "term" }), node("d")],
  edges: [edge("ab", "a", "b"), edge("bc", "b", "c")], truncated: false, total_visible_nodes: 4 };
let mountedProps;
const opened = [];
async function mount(props = {}, { onscreen = true, strict = false } = {}) {
  mountedProps = { data: graph, onOpenNode: (item) => opened.push(item.id), preferenceKey: "test-space", ...props };
  root = createRoot(document.getElementById("root"));
  await act(async () => root.render(React.createElement(strict ? React.StrictMode : React.Fragment, null,
    React.createElement(star.KnowledgeGraph, mountedProps))));
  const svg = document.querySelector("svg.star-surface");
  if (svg) svg.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, right: 960, bottom: 620, width: 960, height: 620 });
  await act(async () => { for (const observer of intersections) observer.emit(onscreen); });
}
async function rerender(patch) {
  mountedProps = { ...mountedProps, ...patch };
  await act(async () => root.render(React.createElement(star.KnowledgeGraph, mountedProps)));
}
async function click(element, detail = 0) { assert.ok(element); await act(async () => element.dispatchEvent(new window.MouseEvent("click", { bubbles: true, detail }))); }
const button = (text) => [...document.querySelectorAll("button")].find((element) => element.textContent.includes(text));
async function input(selector, value, blur = false) {
  const element = document.querySelector(selector); assert.ok(element);
  await act(async () => {
    const prototype = element.tagName === "SELECT" ? window.HTMLSelectElement.prototype : window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value").set.call(element, value);
    element.dispatchEvent(new window.Event(element.tagName === "SELECT" ? "change" : "input", { bubbles: true }));
  });
  if (blur) await act(async () => element.dispatchEvent(new window.FocusEvent("focusout", { bubbles: true })));
}
async function advance(count, start = 10000) {
  await act(async () => { for (let i = 0; i < count; i++) for (const tick of [...clocks]) tick((start + i * 34) / 1000); });
}
async function screenFrame() {
  // v5 pointer/wheel handlers queue actual SVG drawing on the display rAF.
  // Keep the real engine; allow its queued callback to run before reading DOM.
  await act(async () => new Promise(done => window.requestAnimationFrame(done)));
}
async function finishNavigation() {
  for (let index = 0; index < 90; index++) {
    await screenFrame();
    if (surface().dataset.starInteracting !== "true") return;
  }
  assert.fail("camera/120ms idle-window did not finish within 90 display frames");
}
async function pointer(type, target, x, y, id = 1) {
  const event = new window.MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0 });
  Object.defineProperty(event, "pointerId", { value: id });
  await act(async () => target.dispatchEvent(event));
}
function surface() { return document.querySelector("svg.star-surface"); }
function point(id) { return document.querySelector(`[data-star-node="${id}"]`); }
function setReduced(value) {
  const query = window.matchMedia("(prefers-reduced-motion: reduce)"); query.matches = value; query.dispatchEvent(new window.Event("change"));
}
afterEach(async () => {
  if (root) {
    await act(async () => { setReduced(false); root.unmount(); }); root = undefined;
  }
  assert.equal(clocks.size, 0, "graph ticker listener leaked after cleanup");
  assert.equal(intersections.size, 0, "graph intersection observer leaked");
  assert.equal(resizes.size, 0, "graph resize observer leaked");
  assert.deepEqual(errors.splice(0), []);
  visibility = "visible"; focused = true; captures.clear(); opened.length = 0;
  // Only the isolated JSDOM store exists in this test process.
  window.localStorage.clear();
});
after(async () => {
  try { assert.equal(ScrollTrigger.getAll().length, 0); assert.equal(clocks.size, 0); }
  finally {
    // Global plugin infrastructure belongs to this isolated test process, not
    // production component cleanup. Allow stopped d3 timers one final RAF/nap
    // before window.close cancels RAF; otherwise d3's clock-skew interval remains.
    ScrollTrigger.disable();
    gsap.ticker.sleep();
    await new Promise((done) => setTimeout(done, 50));
    gsap.ticker.add = originalAdd; gsap.ticker.remove = originalRemove;
    dom.window.close();
  }
});

test("synthetic fixture is deterministic: exactly 300 nodes and 600 non-dangling unique relationships", () => {
  const data = star.createStarfieldFixture(300);
  assert.deepEqual(data, star.createStarfieldFixture(300));
  assert.equal(data.nodes.length, 300); assert.equal(data.edges.length, 600);
  const ids = new Set(data.nodes.map(n => n.id));
  assert.equal(ids.size, 300); assert.ok(ids.has('perf-299'));
  assert.equal(new Set(data.edges.map(e => e.id)).size, 600);
  assert.equal(new Set(data.edges.map(e => `${e.source}/${e.target}`)).size, 600);
  assert.ok(data.edges.every(e => ids.has(e.source) && ids.has(e.target) && e.source !== e.target));
  assert.equal(data.truncated, false);
});
test("fixture URL count is a fixed allowlist, not an unbounded workload", () => {
  for (const count of [7, 50, 200, 300, 591, 1000]) assert.equal(star.createStarfieldFixture(String(count)).nodes.length, count);
  for (const invalid of [null, '', '300x', 301, 100000, -1, Infinity, NaN]) assert.equal(star.fixtureCount(invalid), 300);
});
test("all graph data survives both preparation and engine validation without a display budget", () => {
  const data = star.createStarfieldFixture(300);
  assert.equal(star.prepareStarGraph(data).nodes.length, 300);
  assert.equal(star.boundedGraph(data, 10000).nodes.length, 300);
  assert.equal(star.boundedGraph(data, 1).nodes.length, 300, 'legacy renderer arguments cannot silently drop nodes');
  const prepared = star.prepareStarGraph(data, 'synthetic-300');
  assert.equal(prepared.nodes.length, 300); assert.equal(prepared.edges.length, 600); assert.equal(prepared.truncated, false);
  const engine = new star.StarGraphEngine(prepared, undefined, undefined, 'synthetic-300');
  try {
    assert.equal(engine.nodes.length, 300); assert.equal(engine.links.length, 600);
    engine.step(1000); assert.ok(engine.phaseTicks <= star.STAR_MAX_TICKS); assert.equal(engine.hot, false);
  } finally { engine.destroy(); }
  const extra = { ...data, nodes: [...data.nodes, node('extra')], edges: [...data.edges, edge('dangling', 'perf-0', 'absent')] };
  const bounded = star.prepareStarGraph(extra, 'synthetic-300');
  assert.equal(bounded.nodes.length, 301); assert.equal(bounded.edges.length, 600); assert.equal(bounded.truncated, true);
});
test("300-node fixture mounts 300 solid dots, 600 paths and opens the final node", async () => {
  await mount({ data: star.createStarfieldFixture(300), renderBudget: 'synthetic-300' });
  assert.equal(document.querySelectorAll('[data-star-node]').length, 300);
  assert.equal(document.querySelectorAll('.star-dot').length, 300);
  assert.equal(document.querySelectorAll('[data-star-edge]').length, 600);
  assert.ok(!document.body.textContent.includes('图谱已截断'));
  await click(point('perf-299'));
  await screenFrame();
  assert.deepEqual(opened, ['perf-299']);
});

test("real d3 physics changes sparse graph positions and cools within 240 ticks", () => {
  const data = { ...graph, nodes: Array.from({ length: 7 }, (_, i) => node(String(i))), edges: Array.from({ length: 4 }, (_, i) => edge(String(i), "0", String(i + 1))) };
  const original = structuredClone(data);
  const engine = new star.StarGraphEngine(data);
  const before = engine.nodes.map((item) => [item.x, item.y]);
  engine.step(20);
  assert.notDeepEqual(engine.nodes.map((item) => [item.x, item.y]), before);
  engine.step(100000);
  assert.equal(engine.hot, false);
  assert.ok(engine.phaseTicks <= star.STAR_MAX_TICKS);
  const final = engine.nodes.map((item) => [item.x, item.y]);
  engine.step(20); assert.deepEqual(engine.nodes.map((item) => [item.x, item.y]), final);
  assert.deepEqual(data, original, "d3 may mutate engine nodes, never API nodes/edges");
  engine.destroy();
});
test("d3 has no autonomous position updates; replay and drag reheat remain bounded", async () => {
  const engine = new star.StarGraphEngine(graph);
  const initial = engine.nodes.map((item) => [item.x, item.y]);
  await new Promise((done) => setTimeout(done, 35));
  assert.deepEqual(engine.nodes.map((item) => [item.x, item.y]), initial);
  assert.equal(engine.totalTicks, 0);
  engine.pin("a", 400, 200); engine.step(10);
  assert.deepEqual([engine.byId.get("a").x, engine.byId.get("a").y], [400, 200]);
  engine.release("a"); engine.step(240); assert.equal(engine.hot, false);
  engine.replay(); assert.deepEqual(engine.nodes.map((item) => [item.x, item.y]), initial);
  engine.destroy(); engine.step(10); assert.equal(engine.destroyed, true);
});
test("graph keeps all valid nodes and relationships without inventing missing endpoints", () => {
  const data = { nodes: Array.from({ length: 210 }, (_, i) => node(String(i))),
    edges: [...Array.from({ length: 810 }, (_, i) => edge(String(i), "0", "1")), edge("missing", "0", "secret")],
    truncated: false, total_visible_nodes: 210 };
  const engine = new star.StarGraphEngine(data);
  assert.equal(engine.nodes.length, 210); assert.equal(engine.links.length, 810); assert.equal(engine.graph.truncated, true);
  assert.ok(!engine.byId.has("secret"));
  engine.settle(); assert.ok(engine.totalTicks <= star.STAR_STATIC_TICKS); assert.equal(engine.hot, false);
  assert.ok(engine.nodes.every((item) => Number.isFinite(item.x) && Number.isFinite(item.y)));
  engine.destroy();
});
test("physical displacement is limited per step and invalid physics cannot create runaway values", () => {
  const engine = new star.StarGraphEngine(graph, { repel: Infinity, center: -10, distance: 10000, nodeSize: NaN });
  for (let i = 0; i < 20; i++) {
    const old = engine.nodes.map((item) => [item.x, item.y]); engine.step();
    engine.nodes.forEach((item, index) => { assert.ok(Math.abs(item.x - old[index][0]) <= 10.001); assert.ok(Math.abs(item.y - old[index][1]) <= 10.001); });
  }
  engine.destroy();
});
test("SVG client/world conversion accounts for letterboxing and zoom preserves pointer anchor", () => {
  assert.deepEqual(star.starClientPoint(300, 250, { left: 0, top: 0, width: 600, height: 500 }), { x: 480, y: 310 });
  assert.equal(star.starClientPoint(0, 0, { left: 0, top: 0, width: 0, height: 0 }), null);
  const camera = { x: 400, y: 300, k: 1.2 }; const anchor = { x: 180, y: 220 };
  const zoomed = star.zoomStarCamera(camera, 2, anchor);
  assert.deepEqual(star.starWorldPoint(anchor, camera), star.starWorldPoint(anchor, zoomed));
  assert.equal(star.zoomStarCamera(camera, 100000).k, 4);
});
test("component frame gate never exceeds its own 30Hz budget or changes global clock settings", () => {
  const gate = star.createStarFrameGate(); let allowed = 0;
  for (let time = 0; time < 1000; time++) if (gate(time, 5000)) allowed++;
  assert.ok(allowed <= 30);
  const active = { paused: false, visible: true, focused: true, onscreen: true, quiet: false };
  assert.equal(star.starCanAnimate(active), true);
  for (const [key, value] of [["paused", true], ["visible", false], ["focused", false], ["onscreen", false], ["quiet", true]])
    assert.equal(star.starCanAnimate({ ...active, [key]: value }), false);
});
test("theme sanitizes colors, strips payloads and bounds per-node overrides/physical settings", () => {
  assert.equal(star.starColor("#AbC"), "#aabbcc");
  for (const invalid of ["red", "url(secret)", "var(--key)", "#fff;", "rgba(0,0,0,0)", 123, null]) assert.equal(star.starColor(invalid), null);
  const normalized = star.normalizeStarTheme({ mode: "unified", unified: "url(secret)", api_key: "never-save", content: "private text",
    groups: { wiki: "#ABCDEF", source: "javascript:x" }, nodes: Object.fromEntries(Array.from({ length: 220 }, (_, i) => [`node${i}`, "#ABC"])),
    physics: { nodeSize: 100, repel: -100, distance: Infinity }, labels: "anything" });
  assert.equal(normalized.groups.wiki, "#abcdef"); assert.ok(Object.keys(normalized.nodes).length <= 200);
  assert.equal(normalized.physics.nodeSize, 1.8); assert.equal(normalized.physics.repel, 35);
  assert.ok(!JSON.stringify(normalized).includes("private text")); assert.ok(!JSON.stringify(normalized).includes("never-save"));
  assert.equal(star.starNodeKey("__proto__"), false);
});
test("preference storage is explicit, isolated, bounded and resilient to blocked localStorage", () => {
  const keyA = star.starPreferenceStorageKey("space-a"); const keyB = star.starPreferenceStorageKey("space-b");
  assert.notEqual(keyA, keyB); assert.equal(star.starPreferenceStorageKey(), null);
  assert.equal(star.starPreferenceStorageKey("bad\nkey"), null);
  const values = new Map(); const storage = { getItem: (key) => values.get(key) || null, setItem: (key, value) => values.set(key, value) };
  const theme = star.defaultStarTheme(); theme.edge = "#123456";
  assert.equal(star.saveStarTheme(keyA, theme, storage), true);
  assert.equal(star.readStarTheme(keyA, storage).edge, "#123456");
  assert.equal(star.readStarTheme(keyB, storage).edge, star.defaultStarTheme().edge);
  const denied = { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); } };
  assert.equal(star.saveStarTheme(keyA, theme, denied), false);
  assert.equal(star.readStarTheme(keyA, denied).mode, "group");
  assert.equal(star.parseStarTheme("x".repeat(50000)).version, 1);
});
test("SVG renders exact real solid points and edges, separated from decorative dust", async () => {
  await mount();
  assert.equal(document.querySelectorAll("g[data-star-node]").length, 4);
  assert.equal(document.querySelectorAll("path[data-star-edge]").length, 2);
  assert.equal(document.querySelectorAll("g[data-star-node] circle.star-dot").length, 4);
  assert.equal(document.querySelectorAll("g[data-decorative='true'] circle").length, 22);
  for (const circle of document.querySelectorAll("circle.star-dot")) { assert.match(circle.getAttribute("fill"), /^#[a-f0-9]{6}$/); assert.ok(Number(circle.getAttribute("r")) > 0); }
  assert.equal(document.querySelectorAll("g[data-star-node] rect, g[data-star-node] polygon").length, 0);
  assert.notEqual(point("b").querySelector(".star-label").style.display, "none", "the connected center keeps its label");
  assert.equal(point("d").querySelector(".star-label").style.display, "none", "orphan auto labels no longer obscure real dots");
});
test("GSAP-driven physics visibly progresses, cools, and replay changes coordinates again", async () => {
  await mount();
  const before = point("a").getAttribute("transform");
  await advance(20);
  assert.notEqual(point("a").getAttribute("transform"), before);
  assert.ok(Number(surface().dataset.starTicks) > 0);
  await advance(240, 20000);
  assert.equal(surface().dataset.starPhase, "settled");
  const finalTicks = surface().dataset.starTicks; const finalPosition = point("a").getAttribute("transform");
  await advance(20, 40000);
  assert.equal(surface().dataset.starTicks, finalTicks); assert.equal(point("a").getAttribute("transform"), finalPosition);
  await click(button("重播收束"));
  assert.equal(surface().dataset.starPhase, "settling");
  assert.notEqual(point("a").getAttribute("transform"), finalPosition);
});
test("hover uses actual adjacency and normal SVG click/accessible open preserve callbacks", async () => {
  await mount();
  await pointer("pointerover", point("a").querySelector(".star-dot"), 100, 100);
  await screenFrame();
  assert.equal(point("b").style.opacity, "1"); assert.equal(point("d").style.opacity, "0.23");
  assert.equal(document.querySelector('[data-star-edge="ab"]').style.opacity, "0.94");
  await click(point("a").querySelector(".star-dot")); assert.deepEqual(opened, ["a"]);
  await click(document.querySelector('button[aria-label="打开真实测试节点 b"]')); assert.deepEqual(opened, ["a", "b"]);
});
test("drag feedback moves an actual point but suppresses the following accidental open", async () => {
  await mount(); await click(button("暂停"));
  const target = point("a").querySelector(".star-dot"); const before = point("a").getAttribute("transform");
  await pointer("pointerdown", target, 480, 310);
  await pointer("pointermove", surface(), 530, 345);
  await screenFrame();
  assert.notEqual(point("a").getAttribute("transform"), before);
  await pointer("pointerup", surface(), 530, 345);
  await click(target, 1); assert.deepEqual(opened, []);
  assert.equal(captures.size, 0);
});
test("drag release clears both React selection and SVG dimming without reopening a node", async () => {
  await mount({ data: star.createStarfieldFixture(300), renderBudget: 'synthetic-300' });
  await click(button("暂停"));
  const target = point('perf-16').querySelector('.star-dot');
  await click(target); assert.ok(point('perf-16').classList.contains('is-selected'));
  assert.equal(document.querySelectorAll('.star-node-directory [aria-pressed="true"]').length, 1);
  await pointer('pointerdown', target, 480, 310);
  await pointer('pointermove', surface(), 530, 345); await screenFrame();
  await pointer('pointerup', surface(), 530, 345); await click(surface(), 1); await screenFrame();
  assert.equal(document.querySelectorAll('[data-star-node].is-selected').length, 0);
  assert.equal(document.querySelectorAll('.star-node-directory [aria-pressed="true"]').length, 0);
  assert.ok([...document.querySelectorAll('[data-star-node]')].every(node => node.style.opacity === '1'));
  assert.ok([...document.querySelectorAll('[data-star-edge]')].every(edge => edge.style.opacity === '0.58'));
  await pointer('pointerover', target, 530, 345); await screenFrame();
  assert.ok([...document.querySelectorAll('[data-star-node]')].every(node => node.style.opacity === '1'));
  assert.deepEqual(opened, ['perf-16']);
});

test("pan/zoom and fit operate independently of physics and remain available while paused", async () => {
  await mount(); await click(button("暂停"));
  const view = point("a").parentElement.parentElement;
  const initial = view.getAttribute("transform");
  await pointer("pointerdown", surface(), 200, 200); await pointer("pointermove", surface(), 250, 230); await pointer("pointerup", surface(), 250, 230);
  assert.notEqual(view.getAttribute("transform"), initial);
  const beforeZoom = Number(view.getAttribute("transform").match(/scale\(([^)]+)\)/)[1]);
  await click(document.querySelector('button[aria-label="放大图谱"]')); await screenFrame();
  const zoomed = view.getAttribute("transform");
  assert.ok(Number(zoomed.match(/scale\(([^)]+)\)/)[1]) > beforeZoom, "zoom must actually be drawn, not just requested");
  await click(button("适应画布")); assert.notEqual(view.getAttribute("transform"), zoomed);
});
test("captured pointerup/click retargeted to the SVG opens the original node exactly once", async () => {
  await mount(); await click(button("暂停"));
  const target = point("a").querySelector(".star-dot");
  await pointer("pointerdown", target, 480, 310);
  await pointer("pointerup", surface(), 480, 310);
  assert.deepEqual(opened, ["a"]);
  assert.ok(point("a").classList.contains("is-selected"));
  await click(surface(), 1);
  assert.deepEqual(opened, ["a"]);
  assert.ok(point("a").classList.contains("is-selected"));
  await click(point("b").querySelector(".star-dot"));
  assert.deepEqual(opened, ["a", "b"], "programmatic click still works without a pointer sequence");
});
test("pointer cancellation never opens a captured point", async () => {
  await mount(); await click(button("暂停"));
  await pointer("pointerdown", point("a").querySelector(".star-dot"), 480, 310);
  await pointer("pointercancel", surface(), 480, 310);
  await click(surface(), 1);
  assert.deepEqual(opened, []); assert.equal(captures.size, 0);
});
test("initial fit reserves labels and never exceeds 1.5 while manual zoom remains available", () => {
  const bounds = { left: -120, right: 150, top: -90, bottom: 110 };
  for (const width of [1100, 760, 330]) {
    const scale = Math.min(width / star.STAR_WIDTH, 570 / star.STAR_HEIGHT);
    const camera = star.fitStarCamera(bounds, star.STAR_WIDTH, star.STAR_HEIGHT, scale);
    assert.ok(camera.k <= 1.5);
    for (const x of [bounds.left, bounds.right]) {
      const label = star.starLabelLayout("运营问题解决方案模板与完整标题", x, 6, camera, scale);
      assert.ok(Math.abs(label.fontSize * camera.k * scale - 12) < 0.01, "label remains about 12 screen pixels");
      assert.ok(Array.from(label.text).length * 12.6 <= label.budget + 0.01);
    }
  }
  assert.equal(star.zoomStarCamera({ x: 480, y: 310, k: 1.5 }, 10).k, 4);
});
test("visibility, blur and offscreen states detach physics clocks and preserve the last complete graph", async () => {
  await mount(); await advance(3);
  for (const kind of ["hidden", "blur", "offscreen"]) {
    await act(async () => {
      if (kind === "hidden") { visibility = "hidden"; document.dispatchEvent(new window.Event("visibilitychange")); }
      if (kind === "blur") { focused = false; window.dispatchEvent(new window.Event("blur")); }
      if (kind === "offscreen") for (const observer of intersections) observer.emit(false);
    });
    assert.equal(clocks.size, 0); assert.equal(surface().dataset.starClock, "stopped");
    const ticks = surface().dataset.starTicks; await advance(4); assert.equal(surface().dataset.starTicks, ticks);
    assert.equal(document.querySelector(".star-edge-layer").style.opacity, "1");
    await act(async () => { visibility = "visible"; focused = true; document.dispatchEvent(new window.Event("visibilitychange"));
      window.dispatchEvent(new window.Event("focus")); for (const observer of intersections) observer.emit(true); });
    assert.equal(clocks.size, 1);
  }
});
test("system reduction overrides rich preference, freezes all clocks, and computes only a bounded static layout", async () => {
  await mount();
  await act(async () => setReduced(true));
  assert.equal(surface().dataset.starPhase, "quiet"); assert.equal(clocks.size, 0);
  const ticks = surface().dataset.starTicks; await advance(100); assert.equal(surface().dataset.starTicks, ticks);
  assert.equal(button("暂停").disabled, true);
  assert.ok(Number(ticks) <= star.STAR_STATIC_TICKS + 2);
});
test("initial reduced/offscreen graph stays complete and never runs a hidden clock", async () => {
  await mount({}, { onscreen: false });
  await act(async () => setReduced(true));
  assert.equal(clocks.size, 0); assert.equal(surface().dataset.starTicks, "0");
  assert.equal(document.querySelector(".star-edge-layer").style.opacity, "1");
  await act(async () => { for (const observer of intersections) observer.emit(true); });
  assert.equal(surface().dataset.starPhase, "quiet"); assert.equal(clocks.size, 0);
});
test("colors persist per space, reject invalid input, and reset only the matching preference", async () => {
  await mount(); await click(button("暂停")); await click(button("配色与布局"));
  await input('input[aria-label="连线颜色色值"]', "#123456", true);
  const key = star.starPreferenceStorageKey("test-space");
  assert.equal(JSON.parse(window.localStorage.getItem(key)).edge, "#123456");
  assert.equal(document.querySelector('[data-star-edge="ab"]').getAttribute("stroke"), "#123456");
  await input('input[aria-label="连线颜色色值"]', "url(x)", true);
  assert.equal(JSON.parse(window.localStorage.getItem(key)).edge, "#123456");
  window.localStorage.setItem("unrelated-ui", "keep");
  await click(button("恢复默认")); assert.equal(window.localStorage.getItem("unrelated-ui"), "keep"); assert.equal(window.localStorage.getItem(key), null);
  await input('input[aria-label="连线颜色色值"]', "#334455", true);
  await rerender({ preferenceKey: "other-space" });
  assert.equal(document.querySelector('input[aria-label="连线颜色色值"]').value, star.defaultStarTheme().edge);
  assert.equal(window.localStorage.getItem(star.starPreferenceStorageKey("other-space")), null);
});
test("uniform/group/per-point and label controls apply without changing graph identities", async () => {
  await mount(); await click(button("暂停")); await click(button("配色与布局"));
  await click(button("统一节点颜色")); await input('input[aria-label="统一节点颜色色值"]', "#224466", true);
  assert.ok([...document.querySelectorAll("circle.star-dot")].every((element) => element.getAttribute("fill") === "#224466"));
  await input('select[aria-label="选择要配色的节点"]', "a"); await input('input[aria-label="选中节点颜色色值"]', "#cc3311", true);
  assert.equal(point("a").querySelector(".star-dot").getAttribute("fill"), "#cc3311");
  assert.equal(document.querySelectorAll("[data-star-node]").length, 4);
  await input('select[aria-label="图谱标签显示"]', "none");
  assert.ok([...document.querySelectorAll(".star-label")].every((element) => element.style.display === "none"));
  assert.equal(document.querySelectorAll('ul[aria-label="图谱节点"] li').length, 4);
});
test("color drawer opens inside the graph, focuses its first field, and restores focus on Escape", async () => {
  await mount();
  const trigger = button("配色与布局");
  await click(trigger);
  const drawer = document.querySelector(".star-settings-drawer");
  assert.equal(drawer.parentElement, document.querySelector(".star-stage"));
  assert.equal(document.activeElement, drawer.querySelector('input[type="text"]'));
  await act(async () => document.activeElement.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
  assert.equal(document.querySelector(".star-settings-drawer"), null);
  assert.equal(document.activeElement, trigger);
});
test("StrictMode cleanup and data removal leave no ticker, observer or revoked node DOM", async () => {
  await mount({}, { strict: true }); assert.equal(clocks.size, 1);
  await rerender({ data: { nodes: [node("a")], edges: [], truncated: false, total_visible_nodes: 1 } });
  await act(async () => { for (const observer of intersections) observer.emit(true); });
  assert.equal(document.querySelectorAll("[data-star-node]").length, 1);
  assert.equal(document.querySelectorAll("[data-star-edge]").length, 0);
  assert.equal(point("b"), null); assert.equal(clocks.size, 1);
});
test("an empty or malformed graph does not invent decorative data nodes", async () => {
  await mount({ data: { nodes: [], edges: [], truncated: false, total_visible_nodes: 0 } });
  assert.equal(document.querySelectorAll("[data-star-node]").length, 0); assert.equal(clocks.size, 0);
  await rerender({ data: { nodes: null, edges: [], truncated: false, total_visible_nodes: 0 } });
  assert.match(document.body.textContent, /数据结构无效/); assert.equal(surface(), null);
});
test("halos are capped at 24 real nodes, inherit colors, and pulse without moving a settled layout", async () => {
  const data = { nodes: Array.from({ length: 40 }, (_, index) => node(String(index))),
    edges: [edge("one", "0", "1")], truncated: false, total_visible_nodes: 40 };
  await mount({ data });
  assert.equal(document.querySelectorAll("circle.star-dot").length, 40);
  assert.equal(document.querySelectorAll("circle.star-halo").length, 24);
  assert.equal(document.querySelectorAll("[data-star-edge]").length, 1);
  await advance(240, 80000);
  assert.equal(surface().dataset.starPhase, "settled");
  const ticks = surface().dataset.starTicks;
  const position = point("0").getAttribute("transform");
  const halo = point("0").querySelector(".star-halo");
  const radius = halo.getAttribute("r");
  const opacity = halo.style.opacity;
  await advance(20, 100000);
  assert.equal(halo.getAttribute("r"), radius, "idle pulse must not invalidate SVG geometry");
  assert.notEqual(halo.style.opacity, opacity);
  assert.equal(surface().dataset.starTicks, ticks); assert.equal(point("0").getAttribute("transform"), position);
  assert.equal(halo.getAttribute("fill"), point("0").querySelector(".star-dot").getAttribute("fill"));
  await click(button("暂停"));
  const frozen = [halo.getAttribute("r"), halo.style.opacity];
  await advance(20, 120000); assert.deepEqual([halo.getAttribute("r"), halo.style.opacity], frozen);
});
test("resize to a narrow viewport preserves screen label size without resetting manual zoom", async () => {
  await mount(); await click(button("暂停"));
  surface().getBoundingClientRect = () => ({ left: 0, top: 0, width: 330, height: 380 });
  await act(async () => { for (const observer of resizes) observer.emit(); });
  const view = point("a").parentElement.parentElement;
  const scale = Number(view.getAttribute("transform").match(/scale\(([^)]+)\)/)[1]);
  const visibleLabel = point("b").querySelector("text");
  assert.notEqual(visibleLabel.style.display, "none", "check a label actually visible under the final auto policy");
  const size = parseFloat(visibleLabel.style.fontSize);
  assert.ok(scale <= 1.5); assert.ok(Math.abs(size * scale * (330 / 960) - 12) < 0.01);
  await click(document.querySelector('button[aria-label="放大图谱"]'));
  await finishNavigation();
  const manual = view.getAttribute("transform");
  assert.ok(Number(manual.match(/scale\(([^)]+)\)/)[1]) > scale);
  await act(async () => { for (const observer of resizes) observer.emit(); });
  assert.equal(view.getAttribute("transform"), manual, "resize must not erase the user's camera");
});
test("a matching storage event applies validated colors and ignores another space", async () => {
  await mount(); await click(button("暂停"));
  const changed = { ...star.defaultStarTheme(), edge: "#224466" };
  await act(async () => window.dispatchEvent(new window.StorageEvent("storage", {
    key: star.starPreferenceStorageKey("other-space"), newValue: JSON.stringify(changed),
  })));
  assert.notEqual(document.querySelector('[data-star-edge="ab"]').getAttribute("stroke"), "#224466");
  await act(async () => window.dispatchEvent(new window.StorageEvent("storage", {
    key: star.starPreferenceStorageKey("test-space"), newValue: JSON.stringify(changed),
  })));
  assert.equal(document.querySelector('[data-star-edge="ab"]').getAttribute("stroke"), "#224466");
});
test("actual stylesheet makes only solid dots interactive, never overlapping labels or halos", async () => {
  await mount();
  const stylesheet = document.createElement("style");
  stylesheet.textContent = readFileSync("src/star-graph.css", "utf8");
  document.head.appendChild(stylesheet);
  try {
    assert.equal(window.getComputedStyle(point("a")).pointerEvents, "none");
    assert.equal(window.getComputedStyle(point("a").querySelector(".star-dot")).pointerEvents, "all");
    assert.equal(window.getComputedStyle(point("a").querySelector(".star-label")).pointerEvents, "none");
    assert.equal(window.getComputedStyle(point("a").querySelector(".star-halo")).pointerEvents, "none");
  } finally { stylesheet.remove(); }
});
test("the final auto label policy reveals hovered nodes and neighbors, with explicit all mode available", async () => {
  await mount(); await click(button("暂停"));
  assert.equal(point("d").querySelector(".star-label").style.display, "none");
  await pointer("pointerover", point("a").querySelector(".star-dot"), 100, 100);
  await screenFrame();
  assert.notEqual(point("a").querySelector(".star-label").style.display, "none");
  assert.notEqual(point("b").querySelector(".star-label").style.display, "none");
  assert.equal(point("c").querySelector(".star-label").style.display, "none");
  await click(button("配色与布局"));
  assert.match(document.querySelector('select[aria-label="图谱标签显示"]').textContent, /自动分级 · 悬停显示标题/);
  await input('select[aria-label="图谱标签显示"]', "all");
  assert.ok([...document.querySelectorAll(".star-label")].every((element) => element.style.display !== "none"));
});
test("large settled graph limits rich cold painting to 8Hz and leaves non-halo dots untouched", async () => {
  const data = { nodes: Array.from({ length: 120 }, (_, index) => node(String(index))),
    edges: Array.from({ length: 119 }, (_, index) => edge(String(index), String(index), String(index + 1))),
    truncated: false, total_visible_nodes: 120 };
  await mount({ data });
  await advance(240, 200000);
  assert.equal(surface().dataset.starPhase, "settled");
  const beforeFrames = Number(surface().dataset.starFrames);
  const ticks = surface().dataset.starTicks;
  const unchanged = point("119").querySelector(".star-dot");
  assert.equal(point("119").querySelector(".star-halo"), null);
  const opacity = unchanged.style.opacity;
  await advance(29, 210000);
  const delta = Number(surface().dataset.starFrames) - beforeFrames;
  assert.ok(delta > 0 && delta <= 8, `expected at most 8 cold paints, received ${delta}`);
  assert.equal(surface().dataset.starTicks, ticks);
  assert.equal(unchanged.style.opacity, opacity);
});
