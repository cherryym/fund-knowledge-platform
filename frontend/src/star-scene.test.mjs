// Component-isolation evidence, not browser latency/FPS evidence. Real React,
// GSAP and d3 run in a private DOM; only geometry/visibility are deterministic.
// Anonymous counters are injected into the in-memory test bundle, never the app.
import test, { after, afterEach } from "node:test";
import assert from "node:assert/strict";
import Module, { createRequire } from "node:module";
import { resolve } from "node:path";
import { readFileSync } from "node:fs";
import { build } from "esbuild";
const require = createRequire(import.meta.url);
const ts = require("typescript");
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
Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
document.hasFocus = () => true;
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
};
globalThis.IntersectionObserver = class {
  constructor(callback) { this.callback = callback; intersections.add(this); }
  observe(target) { this.target = target; }
  disconnect() { intersections.delete(this); }
  emit() { this.callback([{ target: this.target, isIntersecting: true, intersectionRatio: 1 }]); }
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
  const result = originalAdd.call(this, listener, ...args); clocks.add(listener); return result;
};
gsap.ticker.remove = function (listener) { clocks.delete(listener); return originalRemove.call(this, listener); };

const countKeys = ["scene", "svgNode", "svgEdge", "row", "bindNode", "bindEdge", "nodeSet", "nodeDelete", "edgeSet", "edgeDelete"];
const counts = globalThis.__starSceneCounts = Object.fromEntries(countKeys.map((key) => [key, 0]));
const resetCounts = () => countKeys.forEach((key) => { counts[key] = 0; });
const nodeMaps = new Set();
const edgeMaps = new Set();
globalThis.__StarCountingNodeMap = class extends Map {
  constructor() { super(); nodeMaps.add(this); }
  set(key, value) { counts.nodeSet++; return super.set(key, value); }
  delete(key) { counts.nodeDelete++; return super.delete(key); }
};
globalThis.__StarCountingEdgeMap = class extends Map {
  constructor() { super(); edgeMaps.add(this); }
  set(key, value) { counts.edgeSet++; return super.set(key, value); }
  delete(key) { counts.edgeDelete++; return super.delete(key); }
};
function instrumentScene(source, filename) {
  const tree = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const names = new Map([["StarSceneView", "scene"], ["StarSvgNodeView", "svgNode"], ["StarSvgEdgeView", "svgEdge"], ["StarNodeRowView", "row"]]);
  const edits = [];
  const found = [];
  function add(body, key) {
    assert.ok(body && ts.isBlock(body));
    edits.push({ offset: body.getStart(tree) + 1, text: `globalThis.__starSceneCounts.${key}++;` });
    found.push(key);
  }
  function visit(node) {
    if ((ts.isFunctionExpression(node) || ts.isFunctionDeclaration(node)) && node.name && names.has(node.name.text))
      add(node.body, names.get(node.name.text));
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && ["bindNode", "bindEdge"].includes(node.name.text)) {
      assert.ok(ts.isCallExpression(node.initializer));
      add(node.initializer.arguments[0].body, node.name.text);
    }
    ts.forEachChild(node, visit);
  }
  visit(tree);
  assert.deepEqual(found.sort(), ["scene", "svgNode", "svgEdge", "row", "bindNode", "bindEdge"].sort(), "instrument every real render/ref body exactly once");
  for (const edit of edits.sort((a, b) => b.offset - a.offset)) source = source.slice(0, edit.offset) + edit.text + source.slice(edit.offset);
  return source;
}
const result = await build({
  stdin: { contents: `export * from './src/KnowledgeGraph'; export * from './src/StarScene'; export * from './src/starGraphTheme';`,
    resolveDir: process.cwd(), loader: "tsx" },
  bundle: true, write: false, platform: "node", format: "cjs", loader: { ".css": "empty" },
  external: ["react", "react/*", "react-dom", "react-dom/*", "gsap", "gsap/ScrollTrigger", "@gsap/react"],
  plugins: [{ name: "test-only-anonymous-component-counters", setup(builder) {
    builder.onLoad({ filter: /[/\\](StarScene|KnowledgeGraph)\.tsx$/ }, ({ path }) => {
      let contents = readFileSync(path, "utf8");
      if (path.endsWith("StarScene.tsx")) contents = instrumentScene(contents, path);
      else for (const [original, replacement] of [
        ["new Map<string, StarNodeElements>()", "new globalThis.__StarCountingNodeMap()"],
        ["new Map<string, SVGPathElement>()", "new globalThis.__StarCountingEdgeMap()"],
      ]) {
        assert.equal(contents.split(original).length, 2, "instrument the existing ref Map, never replace the engine");
        contents = contents.replace(original, replacement);
      }
      return { contents, loader: "tsx" };
    });
  } }],
});
const filename = resolve("src/__star_scene_test_bundle.cjs");
const compiled = new Module(filename);
compiled.filename = filename; compiled.paths = Module._nodeModulePaths(resolve("src"));
const runtimeRequire = compiled.require.bind(compiled);
compiled.require = (id) => id === "gsap" ? gsap : id === "gsap/ScrollTrigger" ? { ScrollTrigger } : runtimeRequire(id);
compiled._compile(result.outputFiles[0].text, filename);
const star = compiled.exports;
const node = (id) => ({ id, label: `测试星点 ${id}`, kind: "knowledge", knowledge_type: "rule", category: "测试", state: "APPROVED", version_id: null });
const edge = (id, source, target) => ({ id, source, target, type: "WIKI_LINK", origin: "wikilink", state: "ACTIVE" });
const graph = {
  nodes: Array.from({ length: 200 }, (_, i) => node(`n${i}`)),
  edges: Array.from({ length: 400 }, (_, i) => edge(`e${i}`, `n${i % 200}`, `n${(i % 200 + (i < 200 ? 1 : 13)) % 200}`)),
  truncated: false, total_visible_nodes: 200,
};
let root;
let mountedProps;
let strictMode = false;
const opened = [];
const errors = [];
window.addEventListener("error", (event) => errors.push(event.error));
function elementTree() {
  const component = React.createElement(star.KnowledgeGraph, mountedProps);
  return strictMode ? React.createElement(React.StrictMode, null, component) : component;
}
async function mount(props = {}, strict = false) {
  strictMode = strict;
  mountedProps = { data: graph, onOpenNode: (item) => opened.push(item.id), preferenceKey: "scene-test", ...props };
  root = createRoot(document.getElementById("root"));
  await act(async () => root.render(elementTree()));
  const svg = surface();
  if (svg) svg.getBoundingClientRect = () => ({ x: 0, y: 0, left: 0, top: 0, right: 960, bottom: 620, width: 960, height: 620 });
  await act(async () => { for (const observer of intersections) observer.emit(); });
}
async function rerender(patch) {
  mountedProps = { ...mountedProps, ...patch };
  await act(async () => root.render(elementTree()));
}
async function click(element) {
  assert.ok(element);
  await act(async () => element.dispatchEvent(new window.MouseEvent("click", { bubbles: true, detail: 0 })));
}
async function input(selector, value) {
  const element = document.querySelector(selector); assert.ok(element);
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set.call(element, value);
    element.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
}
async function advance(count) {
  await act(async () => {
    for (let i = 0; i < count; i++) for (const tick of [...clocks]) tick((10000 + i * 40) / 1000);
  });
}
const surface = () => document.querySelector("svg.star-surface");
const point = (id) => document.querySelector(`[data-star-node="${id}"]`);
const listButton = (index) => document.querySelectorAll(".star-list-select")[index];
const button = (label) => [...document.querySelectorAll("button")].find((element) => element.textContent.includes(label));
test("height divider grows the mounted stage without rerendering nodes, edges or opening knowledge", async () => {
  await mount({ heightPreferenceKey: "height-scene-test" });
  const handle = document.querySelector('[aria-label="调整图谱高度"]');
  const stage = document.getElementById(handle.getAttribute("aria-controls"));
  const originalHeight = Number(handle.getAttribute("aria-valuenow"));
  assert.ok(originalHeight > 570); assert.equal(handle.getAttribute("aria-orientation"), "horizontal");
  const captured = new Set();
  handle.setPointerCapture = id => captured.add(id); handle.hasPointerCapture = id => captured.has(id);
  handle.releasePointerCapture = id => captured.delete(id);
  const originalSurface = surface(), originalMap = liveNodeMap(); resetCounts();
  await act(async () => {
    for (const [type, y] of [["pointerdown", 500], ["pointermove", 610], ["pointerup", 650]]) {
      const event = new window.Event(type, { bubbles: true, cancelable: true });
      Object.assign(event, { pointerId: 77, clientY: y, button: 0, isPrimary: true }); handle.dispatchEvent(event);
    }
  });
  assert.equal(stage.style.getPropertyValue("--star-stage-height"), `${originalHeight + 150}px`);
  assert.equal(surface(), originalSurface); assert.equal(liveNodeMap(), originalMap); assertMapDOM(originalMap);
  assertSvgUntouched(); assert.equal(counts.row, 0); assert.equal(captured.size, 0); assert.deepEqual(opened, []);
});
function assertSvgUntouched() {
  for (const key of countKeys.filter((key) => key !== "row")) assert.equal(counts[key], 0, `${key} must stay zero on UI-only updates`);
}
function liveNodeMap() {
  const live = [...nodeMaps].filter((map) => map.size); assert.equal(live.length, 1); return live[0];
}
function liveEdgeMap() {
  const live = [...edgeMaps].filter((map) => map.size); assert.equal(live.length, 1); return live[0];
}
function assertMapDOM(map) {
  for (const [id, elements] of map) {
    assert.equal(elements.position, point(id));
    assert.ok(elements.position.isConnected);
    assert.equal(elements.halo, elements.position.querySelector(".star-halo") || undefined);
    assert.equal(elements.dot, elements.position.querySelector(".star-dot"));
  }
}
afterEach(async () => {
  if (root) { await act(async () => root.unmount()); root = undefined; }
  assert.ok([...nodeMaps, ...edgeMaps].every((map) => !map.size), "unmount must clear all cached DOM refs");
  assert.equal(clocks.size, 0, "graph ticker leak");
  assert.equal(intersections.size, 0, "intersection observer leak");
  assert.equal(resizes.size, 0, "resize observer leak");
  assert.deepEqual(errors.splice(0), []);
  nodeMaps.clear(); edgeMaps.clear(); captures.clear(); opened.length = 0; resetCounts();
  window.localStorage.clear();
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)"); reduced.matches = false;
  reduced.dispatchEvent(new window.Event("change"));
});
after(async () => {
  try { assert.equal(ScrollTrigger.getAll().length, 0); assert.equal(clocks.size, 0); }
  finally {
    // The global plugin belongs to this isolated test process only. Let stopped
    // d3 timers finish their final RAF/nap before closing this private window.
    ScrollTrigger.disable(); gsap.ticker.sleep();
    await new Promise((done) => setTimeout(done, 50));
    gsap.ticker.add = originalAdd; gsap.ticker.remove = originalRemove;
    dom.window.close();
    delete globalThis.__starSceneCounts; delete globalThis.__StarCountingNodeMap; delete globalThis.__StarCountingEdgeMap;
  }
});

test("positive control: actual 200-node/400-edge scene renders and registers every real element", async () => {
  await mount();
  assert.equal(counts.scene, 1); assert.equal(counts.svgNode, 200); assert.equal(counts.svgEdge, 400); assert.equal(counts.row, 200);
  assert.equal(counts.bindNode, 200); assert.equal(counts.nodeSet, 200); assert.equal(counts.nodeDelete, 0);
  assert.equal(counts.bindEdge, 400); assert.equal(counts.edgeSet, 400); assert.equal(counts.edgeDelete, 0);
  assert.equal(document.querySelectorAll("[data-star-node]").length, 200);
  assert.equal(document.querySelectorAll("[data-star-edge]").length, 400);
  assert.equal(document.querySelectorAll(".star-halo").length, 24);
  assert.equal(liveNodeMap().size, 200); assert.equal(liveEdgeMap().size, 400); assertMapDOM(liveNodeMap());
  assert.ok([...liveEdgeMap().values()].every((element) => /^M/.test(element.getAttribute("d"))), "real runtime must draw the edges");
});

test("ten list selections: SVG renders/ref callbacks/map writes = 0; only old/new rows render", async () => {
  await mount();
  const before = liveNodeMap();
  let rows = 0;
  for (let index = 0; index < 10; index++) {
    resetCounts(); await click(listButton(index));
    assertSvgUntouched(); assert.equal(counts.row, index ? 2 : 1); rows += counts.row;
    assert.equal(listButton(index).getAttribute("aria-pressed"), "true");
    assert.ok(point(`n${index}`).classList.contains("is-selected"));
  }
  assert.equal(rows, 19); assert.equal(liveNodeMap(), before); assertMapDOM(before);
});

test("real SVG programmatic click fallback updates selection without scene reconciliation", async () => {
  await mount(); resetCounts();
  await click(point("n83").querySelector(".star-dot"));
  assertSvgUntouched(); assert.equal(counts.row, 1); assert.deepEqual(opened, ["n83"]);
  assert.equal(listButton(83).getAttribute("aria-pressed"), "true");
  resetCounts(); await click(point("n84").querySelector(".star-dot"));
  assertSvgUntouched(); assert.equal(counts.row, 2); assert.deepEqual(opened, ["n83", "n84"]);
});

test("pause, resume and runtime phase transition render no SVG and no list rows", async () => {
  await mount(); resetCounts();
  await click(document.querySelector('[aria-label="暂停图谱动态"]'));
  assert.equal(surface().dataset.starPhase, "paused"); assertSvgUntouched(); assert.equal(counts.row, 0);
  resetCounts(); await click(document.querySelector('[aria-label="继续图谱动态"]'));
  assertSvgUntouched(); assert.equal(counts.row, 0);
  resetCounts(); await advance(260);
  assert.equal(surface().dataset.starPhase, "settled"); assertSvgUntouched(); assert.equal(counts.row, 0);
});

test("a new external open callback stays fresh without invalidating memo scene or rows", async () => {
  await mount(); const replacementCalls = []; resetCounts();
  await rerender({ onOpenNode: (item) => replacementCalls.push(item.id), label: "更新图谱标题" });
  assertSvgUntouched(); assert.equal(counts.row, 0);
  await click(document.querySelectorAll(".star-list-open")[42]);
  assert.deepEqual(replacementCalls, ["n42"]); assert.deepEqual(opened, []); assertSvgUntouched(); assert.equal(counts.row, 0);
  await click(point("n43").querySelector(".star-dot"));
  assert.deepEqual(replacementCalls, ["n42", "n43"]);
});

test("persisted single-node color changes repaint through the runtime, not SVG reconciliation", async () => {
  await mount(); resetCounts();
  const next = star.defaultStarTheme(); next.nodes.n83 = "#ab1234";
  const key = star.starPreferenceStorageKey("scene-test");
  await act(async () => {
    window.localStorage.setItem(key, JSON.stringify(next));
    window.dispatchEvent(new window.StorageEvent("storage", { key, newValue: JSON.stringify(next) }));
  });
  assertSvgUntouched(); assert.equal(counts.row, 1);
  assert.equal(point("n83").querySelector(".star-dot").getAttribute("fill"), "#ab1234");
  assert.equal(listButton(83).querySelector("i").style.background, "rgb(171, 18, 52)");
});

test("settings and list filtering preserve the scene and unchanged list row identities", async () => {
  await mount(); const retained = listButton(83); resetCounts();
  await click(button("配色与布局")); assertSvgUntouched(); assert.equal(counts.row, 0);
  assert.ok(document.querySelector('[aria-label="图谱配色与布局设置"]'));
  await input('[aria-label="筛选图谱节点列表"]', "n83");
  assertSvgUntouched(); assert.equal(counts.row, 0);
  assert.equal(document.querySelectorAll(".star-list-select").length, 1);
  assert.equal(document.querySelector(".star-list-select"), retained);
});

test("real data title changes update a single node/row without ref rebinding", async () => {
  await mount(); const before = point("n83"); resetCounts();
  const next = { ...graph, nodes: graph.nodes.map((item) => item.id === "n83" ? { ...item, label: "新的标题" } : item) };
  await rerender({ data: next });
  assert.equal(counts.scene, 1); assert.equal(counts.svgNode, 1); assert.equal(counts.svgEdge, 0); assert.equal(counts.row, 1);
  for (const key of ["bindNode", "bindEdge", "nodeSet", "nodeDelete", "edgeSet", "edgeDelete"]) assert.equal(counts[key], 0);
  assert.equal(point("n83"), before); assert.equal(before.querySelector("title").textContent, "新的标题");
  assert.equal(listButton(83).querySelector("span").textContent, "新的标题"); assertMapDOM(liveNodeMap());
});

test("topology removals clean cached refs and halo rank changes never leave detached halo pointers", async () => {
  const sparse = { nodes: graph.nodes.slice(0, 30), edges: [], truncated: false, total_visible_nodes: 30 };
  await mount({ data: sparse }); const map = liveNodeMap();
  assert.ok(map.get("n23").halo); assert.equal(map.get("n29").halo, undefined); resetCounts();
  await rerender({ data: { ...sparse, edges: [edge("new", "n29", "n0")] } });
  assert.equal(map.get("n23").halo, undefined); assert.ok(map.get("n29").halo?.isConnected);
  assert.equal(counts.nodeDelete, 2); assert.equal(counts.nodeSet, 2); assertMapDOM(map);
  resetCounts();
  await rerender({ data: { ...sparse, nodes: sparse.nodes.slice(0, 29), edges: [] } });
  assert.equal(map.has("n29"), false); assert.equal(point("n29"), null);
  assert.equal(document.querySelectorAll("[data-star-node]").length, 29);
  assert.equal(document.querySelectorAll("[data-star-edge]").length, 0);
  assert.ok([...edgeMaps].every((entry) => entry.size === 0)); assertMapDOM(map);
  await rerender({ data: { nodes: [], edges: [], truncated: false, total_visible_nodes: 0 } });
  assert.equal(surface(), null); assert.equal(map.size, 0);
});

test("StrictMode and reduced motion keep cached refs valid and UI updates isolated", async () => {
  const query = window.matchMedia("(prefers-reduced-motion: reduce)"); query.matches = true;
  await mount({}, true);
  assert.equal(surface().dataset.starPhase, "quiet"); assertMapDOM(liveNodeMap()); resetCounts();
  await click(listButton(5));
  assertSvgUntouched(); assert.ok(counts.row <= 2, "StrictMode may render the single changed row twice");
  assert.equal(listButton(5).getAttribute("aria-pressed"), "true");
  await rerender({ data: { ...graph, nodes: graph.nodes.slice(0, 199), edges: graph.edges.filter((item) => item.source !== "n199" && item.target !== "n199") } });
  assert.equal(liveNodeMap().size, 199); assertMapDOM(liveNodeMap());
});
