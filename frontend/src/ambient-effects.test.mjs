// node --test src/ambient-effects.test.mjs
// Project jsdom@26.1.0; real React/GSAP; deterministic ticker, Canvas2D, geometry,
// visibility, intersection and long-task stand-ins. No browser or hardware-FPS claim.
import test, { after, afterEach } from "node:test";
import assert from "node:assert/strict";
import Module, { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { readFile } from "node:fs/promises";
import { build } from "esbuild";
const require = createRequire(import.meta.url);
const { JSDOM } = require("jsdom");
assert.equal(require("jsdom/package.json").version, "26.1.0");
const dom = new JSDOM(
  '<!doctype html><html><body><div id="root"></div></body></html>',
  { url: "http://localhost/", pretendToBeVisual: true },
);
const { window } = dom;
const { document } = window;
for (const key of [
  "window",
  "document",
  "HTMLElement",
  "HTMLDialogElement",
  "HTMLCanvasElement",
  "SVGElement",
  "Element",
  "Node",
  "MutationObserver",
  "Event",
  "MouseEvent",
  "FocusEvent",
  "StorageEvent",
])
  globalThis[key] = key === "window" ? window : window[key];
globalThis.requestAnimationFrame = window.requestAnimationFrame.bind(window);
globalThis.cancelAnimationFrame = window.cancelAnimationFrame.bind(window);
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let reduced = false;
let coarse = false;
let focused = true;
let visibility = "visible";
let rectReads = 0;
Object.defineProperty(document, "visibilityState", {
  configurable: true,
  get: () => visibility,
});
document.hasFocus = () => focused;
const mediaQueries = new Map();
const mediaMatches = (query) =>
  query.includes("prefers-reduced-motion")
    ? query.includes("no-preference")
      ? !reduced
      : reduced
    : query.includes("coarse")
      ? coarse
      : query.includes("fine")
        ? !coarse
        : false;
window.matchMedia = (query) => {
  if (!mediaQueries.has(query)) {
    const target = new window.EventTarget();
    target.media = query;
    target.matches = mediaMatches(query);
    target.addListener = (callback) =>
      target.addEventListener("change", callback);
    target.removeListener = (callback) =>
      target.removeEventListener("change", callback);
    mediaQueries.set(query, target);
  }
  return mediaQueries.get(query);
};
function mediaChanged() {
  for (const [query, target] of mediaQueries) {
    target.matches = mediaMatches(query);
    target.dispatchEvent(new window.Event("change"));
  }
}
const nativeStyle = window.getComputedStyle.bind(window);
window.getComputedStyle = (node, pseudo) =>
  new Proxy(nativeStyle(node, pseudo), {
    get(target, property) {
      if (property === "getPropertyValue")
        return (name) => {
          const value = target.getPropertyValue(name);
          return ["translate", "rotate", "scale"].includes(name) && value === ""
            ? "none"
            : value;
        };
      const value = Reflect.get(target, property, target);
      return typeof value === "function"
        ? value.bind(target)
        : ["translate", "rotate", "scale"].includes(property) && value === ""
          ? "none"
          : value;
    },
  });
globalThis.getComputedStyle = window.getComputedStyle;
window.HTMLElement.prototype.getBoundingClientRect = function () {
  rectReads++;
  const card = this.matches(".models-provider-card");
  const top = Number(this.dataset.testTop ?? 40);
  const width =
    this.tagName === "BUTTON" ? 100 : card ? 270 : window.innerWidth;
  const height =
    this.tagName === "BUTTON" ? 40 : card ? 160 : window.innerHeight;
  return {
    x: 20,
    y: top,
    left: 20,
    top,
    width,
    height,
    right: 20 + width,
    bottom: top + height,
    toJSON() {},
  };
};
Object.defineProperty(window.HTMLElement.prototype, "inert", {
  configurable: true,
  get() {
    return this.hasAttribute("inert");
  },
  set(value) {
    this.toggleAttribute("inert", value);
  },
});
class IntersectionStandIn {
  static all = new Set();
  constructor(callback) {
    this.callback = callback;
    this.targets = new Set();
    IntersectionStandIn.all.add(this);
  }
  observe(target) {
    this.targets.add(target);
  }
  unobserve(target) {
    this.targets.delete(target);
  }
  disconnect() {
    this.targets.clear();
    IntersectionStandIn.all.delete(this);
  }
  emit(target, isIntersecting) {
    this.callback([
      { target, isIntersecting, intersectionRatio: isIntersecting ? 1 : 0 },
    ]);
  }
}
class ResizeStandIn {
  static all = new Set();
  constructor(callback) {
    this.callback = callback;
    this.targets = new Set();
    ResizeStandIn.all.add(this);
  }
  observe(target) {
    this.targets.add(target);
  }
  disconnect() {
    this.targets.clear();
    ResizeStandIn.all.delete(this);
  }
}
class LongTaskStandIn {
  static all = new Set();
  constructor(callback) {
    this.callback = callback;
    LongTaskStandIn.all.add(this);
  }
  observe() {}
  disconnect() {
    LongTaskStandIn.all.delete(this);
  }
  emit(durations) {
    this.callback({
      getEntries: () => durations.map((duration) => ({ duration })),
    });
  }
}
window.IntersectionObserver = IntersectionStandIn;
window.ResizeObserver = ResizeStandIn;
window.PerformanceObserver = LongTaskStandIn;
class ManualClock {
  time = 0;
  listeners = new Set();
  added = 0;
  removed = 0;
  now = () => this.time;
  add = (callback) => {
    this.listeners.add(callback);
    this.added++;
  };
  remove = (callback) => {
    this.listeners.delete(callback);
    this.removed++;
  };
  at(time) {
    this.time = time;
    for (const callback of [...this.listeners]) callback();
  }
  advance(delta) {
    this.at(this.time + delta);
  }
}
const canvasContexts = new WeakMap();
function canvasRecorder(clock) {
  const state = {
    clears: 0,
    fills: 0,
    strokes: 0,
    frameStrokes: 0,
    maxStrokes: 0,
    cost: 0,
  };
  return {
    state,
    setTransform() {},
    clearRect() {
      state.clears++;
      state.frameStrokes = 0;
      if (clock) clock.time += state.cost;
    },
    beginPath() {},
    moveTo(x, y) {
      assert.ok(Number.isFinite(x) && Number.isFinite(y));
    },
    lineTo(x, y) {
      assert.ok(Number.isFinite(x) && Number.isFinite(y));
    },
    arc(x, y, r) {
      assert.ok(Number.isFinite(x) && Number.isFinite(y) && r > 0);
    },
    fill() {
      state.fills++;
    },
    stroke() {
      state.strokes++;
      state.frameStrokes++;
      state.maxStrokes = Math.max(state.maxStrokes, state.frameStrokes);
    },
  };
}
window.HTMLCanvasElement.prototype.getContext = function () {
  if (!canvasContexts.has(this)) canvasContexts.set(this, canvasRecorder());
  return canvasContexts.get(this);
};
const runtimeErrors = [];
window.addEventListener("error", (event) =>
  runtimeErrors.push(event.error ?? new Error(event.message)),
);
const React = require("react");
const { act } = React;
const h = React.createElement;
const { createRoot } = require("react-dom/client");
const gsap = require("gsap").gsap;
const compiled = await build({
  stdin: {
    contents:
      'export * from "./AmbientEffects"; export * from "./DocumentHeadingEffects"; export * from "./motionPreferences"; export * from "./visualInteraction";',
    resolveDir: resolve("src"),
    loader: "tsx",
  },
  bundle: true,
  platform: "node",
  format: "cjs",
  plugins: [{
    name: "ambient-lifecycle-test-probe",
    setup(builder) {
      builder.onLoad({ filter: /[/\\]visualInteraction\.ts$/ }, async ({ path }) => {
        const source = await readFile(path, "utf8");
        assert.match(source, /const subscriptions = new Set<Subscription>/);
        return {
          // Count actual retained subscriptions; no cleanup mock or production API.
          contents: source + "\nexport function inspectGraphSubscriptionsForTest() { return subscriptions.size; }",
          loader: "ts",
        };
      });
      builder.onLoad({ filter: /[/\\]AmbientEffects\.tsx$/ }, async ({ path }) => {
        const source = await readFile(path, "utf8");
        const anchor = "getStats: () => ({ ...stats }),";
        assert.equal(source.split(anchor).length, 2, "the private-state probe must have one insertion point");
        // Test-bundle only: inspect reference presence without retaining nodes,
        // replacing cleanup, or expanding the production runtime's public API.
        return {
          contents: source.replace(anchor, `${anchor}
            inspectInteractionForTest: () => ({
              pointerCard: pointerCard !== null,
              pointerButton: pointerButton !== null,
              activeCard: activeCard !== undefined,
              cardBounds: cardBounds !== undefined,
              magnet: magnet !== undefined,
              pointerActive: pointer.active,
            }),`),
          loader: "tsx",
        };
      });
    },
  }],
  external: [
    "react",
    "react-dom",
    "react-dom/*",
    "react/jsx-runtime",
    "gsap",
    "@gsap/react",
  ],
  write: false,
  loader: {".css":"empty"},
  logLevel: "silent",
});
const loaded = new Module(resolve("src/ambient-test-runtime.cjs"));
loaded.filename = resolve("src/ambient-test-runtime.cjs");
loaded.paths = Module._nodeModulePaths(dirname(loaded.filename));
const runtimeRequire = loaded.require.bind(loaded);
loaded.require = (id) => (id === "gsap" ? gsap : runtimeRequire(id));
loaded._compile(compiled.outputFiles[0].text, loaded.filename);
const {
  AmbientEffects,
  DocumentHeadingEffects,
  AmbientFrameBudget,
  createAmbientRuntime,
  clampMagneticOffset,
  useMotionPreferences,
  MOTION_PREFERENCE_KEY,
  AMBIENT_LIMITS,
  setGraphInteraction,
  isGraphInteracting,
  inspectGraphSubscriptionsForTest,
} = loaded.exports;
let reactRoot;
const controllers = [];
const graphOwners = new Set();
function graphOwner() {
  const owner = {};
  graphOwners.add(owner);
  return owner;
}
const seenPreferences = new Map();
function Pref({ name }) {
  const preference = useMotionPreferences();
  seenPreferences.set(name, preference);
  return h(
    "output",
    {
      "data-pref": name,
      "data-level": preference.level,
      "data-effective": preference.effective,
    },
    preference.effective,
  );
}
async function mount(element) {
  reactRoot = createRoot(document.getElementById("root"));
  await act(async () => reactRoot.render(element));
}
const wait = (ms) => act(() => new Promise((done) => setTimeout(done, ms)));
function makeRuntime({
  level = "rich",
  mobile = false,
  surfaceCount = 16,
  host = false,
} = {}) {
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    value: mobile ? 390 : 1487,
  });
  Object.defineProperty(window, "innerHeight", {
    configurable: true,
    value: mobile ? 844 : 1058,
  });
  coarse = mobile;
  mediaChanged();
  const root = document.createElement("div");
  root.className = "app-shell";
  root.innerHTML =
    '<div class="ambient-effects"><div class="ambient-aurora"></div><div class="ambient-aurora"></div><div class="ambient-aurora"></div><canvas></canvas></div><div class="ambient-trail-layer">' +
    "<span></span>".repeat(8) +
    '</div><div class="main-shell"><header class="page-heading"><h1>测试标题</h1><button class="primary" id="cta"><svg></svg>创建</button><button class="primary danger" id="danger"><svg></svg>删除</button></header>' +
    (host ? '<div class="topbar-right"></div>' : "") +
    '<div class="cards">' +
    Array.from(
      { length: surfaceCount },
      (_, i) =>
        '<article class="models-provider-card" id="card-' +
        i +
        '">模型 ' +
        i +
        "</article>",
    ).join("") +
    '</div><form><input id="field"><button class="primary" id="form-button"><svg></svg>提交</button></form><div class="document-workbench"><div class="segment-handle" id="drag" draggable="true">拖动</div><div contenteditable="true" id="editor">正文</div></div><table><tbody><tr id="row"><td>真实数据</td></tr></tbody></table><div class="wiki-graph" id="graph">真实图谱</div></div>';
  document.body.append(root);
  const layer = root.querySelector(".ambient-effects");
  const canvas = root.querySelector("canvas");
  const clock = new ManualClock();
  const graphics = canvasRecorder(clock);
  canvasContexts.set(canvas, graphics);
  const reports = [];
  const controller = createAmbientRuntime({
    root,
    layer,
    canvas,
    auroras: [...root.querySelectorAll(".ambient-aurora")],
    trailNodes: [...root.querySelector(".ambient-trail-layer").children],
    level,
    seed: 123,
    clock,
    onState: (state) => reports.push(state),
  });
  controllers.push({ controller, root });
  return { root, layer, canvas, clock, graphics, controller, reports };
}
function pointer(node, x = 99, y = 69, overrides = {}) {
  const event = new window.Event("pointermove", {
    bubbles: true,
    cancelable: true,
  });
  Object.assign(event, {
    pointerType: "mouse",
    clientX: x,
    clientY: y,
    buttons: 0,
    ...overrides,
  });
  node.dispatchEvent(event);
  return event;
}
afterEach(async () => {
  for (const { controller, root } of controllers.splice(0)) {
    controller.dispose();
    root.remove();
  }
  if (reactRoot) {
    await act(async () => reactRoot.unmount());
    reactRoot = undefined;
  }
  for (const owner of graphOwners) setGraphInteraction(owner, false);
  graphOwners.clear();
  assert.equal(isGraphInteracting(), false);
  assert.equal(inspectGraphSubscriptionsForTest(), 0);
  await wait(100);
  reduced = false;
  coarse = false;
  focused = true;
  visibility = "visible";
  mediaChanged();
  window.localStorage.clear();
  window.dispatchEvent(
    new window.StorageEvent("storage", {
      key: MOTION_PREFERENCE_KEY,
      newValue: "rich",
    }),
  );
  assert.equal(IntersectionStandIn.all.size, 0);
  assert.equal(ResizeStandIn.all.size, 0);
  assert.equal(LongTaskStandIn.all.size, 0);
  assert.deepEqual(
    runtimeErrors.splice(0).map((error) => error.message),
    [],
  );
});
after(() => {
  gsap.ticker.sleep();
  window.close();
});

test("motion defaults to rich and same-page consumers update without polling", async () => {
  await mount(
    h(React.Fragment, null, h(Pref, { name: "one" }), h(Pref, { name: "two" })),
  );
  assert.equal(seenPreferences.get("one").level, "rich");
  await act(() => seenPreferences.get("one").setLevel("balanced"));
  assert.equal(seenPreferences.get("two").effective, "balanced");
  assert.equal(window.localStorage.getItem(MOTION_PREFERENCE_KEY), "balanced");
  assert.equal(window.localStorage.length, 1);
});
test("system reduction forces quiet, preserves chosen level and propagates storage events", async () => {
  await mount(h(Pref, { name: "system" }));
  await act(() => {
    reduced = true;
    mediaChanged();
  });
  assert.equal(seenPreferences.get("system").level, "rich");
  assert.equal(seenPreferences.get("system").effective, "quiet");
  await act(() =>
    window.dispatchEvent(
      new window.StorageEvent("storage", {
        key: MOTION_PREFERENCE_KEY,
        newValue: "balanced",
      }),
    ),
  );
  assert.equal(seenPreferences.get("system").level, "balanced");
  assert.equal(seenPreferences.get("system").effective, "quiet");
  await act(() => {
    reduced = false;
    mediaChanged();
  });
  assert.equal(seenPreferences.get("system").effective, "balanced");
});
test("denied localStorage never breaks UI and invalid levels are not persisted", async () => {
  await mount(h(Pref, { name: "denied" }));
  const descriptor = Object.getOwnPropertyDescriptor(window, "localStorage");
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    get() {
      throw new Error("storage denied");
    },
  });
  try {
    await act(() => seenPreferences.get("denied").setLevel("quiet"));
    assert.equal(seenPreferences.get("denied").effective, "quiet");
    await act(() => seenPreferences.get("denied").setLevel("unsupported"));
    assert.equal(seenPreferences.get("denied").level, "quiet");
  } finally {
    Object.defineProperty(window, "localStorage", descriptor);
  }
  await act(() => seenPreferences.get("denied").setLevel("rich"));
});
test("30Hz throttling is not mistaken for overload; resume clears stale delta samples", () => {
  const governor = new AmbientFrameBudget("rich", false);
  let painted = 0;
  for (let i = 0; i < 600; i++)
    if (governor.accept((i * 1000) / 60)) {
      painted++;
      governor.record(0.8);
    }
  assert.equal(painted, 300);
  assert.equal(governor.budget.particles, 72);
  assert.equal(governor.degraded, false);
  governor.resetClock();
  assert.equal(governor.accept(600000), true);
  governor.record(0.8);
  assert.ok(governor.lastGap < 34);
  assert.equal(governor.degraded, false);
});
test("sustained expensive painting reduces density and FPS within hard limits", () => {
  const governor = new AmbientFrameBudget("rich", false);
  for (let i = 0; i < 100; i++) {
    governor.accept(i * 100);
    governor.record(22);
  }
  assert.equal(governor.budget.quality, "economy");
  assert.equal(governor.budget.fps, 15);
  assert.ok(governor.budget.particles < 72);
  const mobile = new AmbientFrameBudget("rich", true);
  assert.equal(mobile.budget.particles, 24);
  const quiet = new AmbientFrameBudget("quiet", false);
  assert.equal(quiet.accept(1), false);
  assert.equal(quiet.budget.fps, 0);
});
test("rich scene renders full density, bounded links and no per-frame DOM discovery or React-state reporting", () => {
  const { root, canvas, clock, graphics, controller, reports } = makeRuntime();
  clock.at(0);
  const readCount = rectReads;
  const reportCount = reports.length;
  let queries = 0;
  const original = root.querySelectorAll.bind(root);
  root.querySelectorAll = (...args) => {
    queries++;
    return original(...args);
  };
  for (let i = 1; i < 60; i++) clock.at((i * 1000) / 60);
  const stats = controller.getStats();
  assert.equal(stats.particles, 72);
  assert.equal(stats.frames, 30);
  assert.ok(stats.links > 30 && stats.links <= 110);
  assert.ok(graphics.state.maxStrokes <= 110);
  assert.equal(queries, 0);
  assert.equal(rectReads, readCount);
  assert.equal(reports.length, reportCount);
  assert.ok(stats.surfaces <= 12);
  assert.equal(canvas.dataset.particleCount, "72");
  assert.equal(root.dataset.ambientShell, "");
});
test("blur, visibility and offscreen pause the private clock; resume does not downgrade", () => {
  const { root, clock, controller } = makeRuntime();
  clock.at(0);
  const count = controller.getStats().frames;
  window.dispatchEvent(new window.Event("blur"));
  assert.equal(clock.listeners.size, 0);
  clock.at(60000);
  assert.equal(controller.getStats().frames, count);
  window.dispatchEvent(new window.Event("focus"));
  assert.equal(clock.listeners.size, 1);
  clock.advance(1);
  assert.equal(controller.getStats().degraded, false);
  visibility = "hidden";
  document.dispatchEvent(new window.Event("visibilitychange"));
  assert.equal(clock.listeners.size, 0);
  visibility = "visible";
  document.dispatchEvent(new window.Event("visibilitychange"));
  assert.equal(clock.listeners.size, 1);
  const io = [...IntersectionStandIn.all].find((instance) =>
    instance.targets.has(root),
  );
  io.emit(root, false);
  assert.equal(clock.listeners.size, 0);
  clock.advance(50000);
  io.emit(root, true);
  clock.advance(1);
  assert.equal(controller.getStats().quality, "rich");
});
test("offscreen cards stop transforms; long tasks trigger scoped degradation", () => {
  const { root, clock, controller } = makeRuntime();
  const card = root.querySelector("#card-0");
  const light = card.querySelector(".ambient-card-edge-light");
  clock.at(0);
  const transform = light.style.transform;
  const io = [...IntersectionStandIn.all].find((instance) =>
    instance.targets.has(card),
  );
  io.emit(card, false);
  clock.advance(100);
  assert.equal(light.style.transform, transform);
  [...LongTaskStandIn.all][0].emit([95, 100, 120]);
  assert.equal(controller.getStats().degraded, true);
  assert.equal(controller.getStats().quality, "balanced");
});
test("trail is capped at eight and inputs, drags, table rows and graph pointers stay untouched", () => {
  const { root, clock, controller } = makeRuntime();
  for (let i = 0; i < 50; i++) {
    pointer(root.querySelector(".page-heading"), 30 + i * 8, 45 + i);
    clock.advance(34);
    assert.ok(controller.getStats().trailPoints <= 8);
  }
  assert.equal(root.querySelector(".ambient-trail-layer").children.length, 8);
  for (const id of ["field", "editor", "drag", "row", "graph"]) {
    const event = pointer(root.querySelector("#" + id));
    clock.advance(34);
    assert.equal(event.defaultPrevented, false);
    assert.equal(controller.getStats().trailPoints, 0);
    assert.equal(root.querySelector("#" + id).style.transform, "");
  }
  const event = pointer(root.querySelector(".page-heading"), 60, 80, {
    buttons: 1,
  });
  clock.advance(34);
  assert.equal(event.defaultPrevented, false);
  assert.equal(controller.getStats().trailPoints, 0);
});
test("magnetic offset is at most four pixels and moves only safe CTA icons, never button hitboxes", () => {
  assert.ok(
    Math.hypot(...Object.values(clampMagneticOffset(20, 20))) <= 4.00001,
  );
  const output = { x: 1, y: 2 };
  assert.equal(clampMagneticOffset(2, 3, output), output);
  const { root, clock } = makeRuntime();
  const button = root.querySelector("#cta");
  pointer(button, 115, 76);
  clock.at(0);
  for (const tween of gsap.globalTimeline.getChildren(true, true, false))
    if (tween.targets().includes(button.querySelector("svg")))
      tween.progress(1);
  const icon = button.querySelector("svg");
  const x = parseFloat(icon.style.getPropertyValue("--ambient-magnet-x"));
  const y = parseFloat(icon.style.getPropertyValue("--ambient-magnet-y"));
  assert.ok(Math.hypot(x, y) > 0 && Math.hypot(x, y) <= 4.0001);
  assert.equal(button.style.transform, "");
  pointer(root.querySelector("#danger"));
  clock.advance(34);
  assert.equal(
    root.querySelector("#danger").hasAttribute("data-ambient-magnetic"),
    false,
  );
  pointer(root.querySelector("#form-button"));
  clock.advance(34);
  assert.equal(
    root.querySelector("#form-button").hasAttribute("data-ambient-magnetic"),
    false,
  );
});
test("mobile limits density to 24 and touch pointers do not gain magnetic interaction", () => {
  const { root, clock, controller } = makeRuntime({ mobile: true });
  assert.equal(controller.getStats().particles, 24);
  pointer(root.querySelector("#cta"), 50, 50, { pointerType: "touch" });
  clock.at(0);
  assert.equal(
    root.querySelector("#cta").hasAttribute("data-ambient-magnetic"),
    false,
  );
  assert.equal(controller.getStats().trailPoints, 0);
});
test("repeated CTA hover does not retain killed tweens in the long-lived route context", () => {
  const original = gsap.context;
  const contexts = [];
  gsap.context = (...args) => {
    const value = original(...args);
    contexts.push({ value, scope: args[1] });
    return value;
  };
  try {
    const { root, clock, controller } = makeRuntime();
    const routeContext = contexts.find((item) => item.scope === root).value;
    const baseline = routeContext.getTweens().length;
    for (let i = 0; i < 40; i++) {
      pointer(root.querySelector("#cta"));
      clock.advance(34);
      root.dispatchEvent(new window.Event("pointerleave"));
      clock.advance(34);
    }
    assert.equal(routeContext.getTweens().length, baseline);
    for (const item of contexts.filter(
      (item) => item.scope instanceof window.SVGElement,
    ))
      assert.equal(item.value.getTweens().length, 0);
    controller.dispose();
    assert.equal(
      root.querySelector("#cta").hasAttribute("data-ambient-magnetic"),
      false,
    );
  } finally {
    gsap.context = original;
  }
});
test("quiet initializes no loop and disposal removes own observers/decorations without killing unrelated GSAP", () => {
  const { root, clock, controller } = makeRuntime({ level: "quiet" });
  assert.equal(clock.listeners.size, 0);
  assert.equal(controller.getStats().fpsCap, 0);
  assert.equal(root.querySelectorAll(".ambient-card-edge").length, 0);
  const marker = { value: 0 };
  const unrelated = gsap.to(marker, { value: 1, duration: 5 });
  controller.dispose();
  assert.ok(unrelated.parent);
  unrelated.kill();
  assert.equal(root.hasAttribute("data-ambient-shell"), false);
  assert.equal(clock.listeners.size, 0);
});
test("removing cards clears decorators and disposing leaves no callbacks or added surface DOM", async () => {
  const { root, clock, controller } = makeRuntime();
  const card = root.querySelector("#card-0");
  card.remove();
  await wait(100);
  assert.equal(card.querySelector(".ambient-card-edge"), null);
  controller.dispose();
  const before = controller.getStats().frames;
  pointer(root);
  clock.advance(100);
  window.dispatchEvent(new window.Event("focus"));
  assert.equal(clock.listeners.size, 0);
  assert.equal(controller.getStats().frames, before);
  assert.equal(
    root.querySelectorAll(
      ".ambient-card-edge,.ambient-card-sheen,.ambient-title-glint",
    ).length,
    0,
  );
  assert.equal(IntersectionStandIn.all.size, 0);
  assert.equal(ResizeStandIn.all.size, 0);
  assert.equal(LongTaskStandIn.all.size, 0);
});
const clearedInteraction = {
  pointerCard: false,
  pointerButton: false,
  activeCard: false,
  cardBounds: false,
  magnet: false,
  pointerActive: false,
};
test("detached hovered Wiki card releases all interaction references before 100 stationary frames", async () => {
  const { root, clock, controller } = makeRuntime({ surfaceCount: 0 });
  const wiki = document.createElement("ul");
  wiki.className = "wiki-page-index";
  wiki.innerHTML = '<li><button class="primary"><svg></svg><span>待撤回的知识摘要</span></button></li>';
  root.querySelector(".cards").append(wiki);
  await wait(100);
  const card = wiki.querySelector("button");
  pointer(card);
  clock.at(0);
  assert.deepEqual(controller.inspectInteractionForTest(), {
    pointerCard: true, pointerButton: true, activeCard: true,
    cardBounds: true, magnet: true, pointerActive: true,
  });
  wiki.remove();
  // Let discovery drop the surface, with no pointer event or animation tick.
  await wait(100);
  assert.equal(controller.getStats().surfaces, 0);
  assert.equal(card.querySelector(".ambient-card-edge"), null);
  assert.deepEqual(controller.inspectInteractionForTest(), clearedInteraction);
  for (let frame = 0; frame < 100; frame++) clock.advance(34);
  assert.deepEqual(controller.inspectInteractionForTest(), clearedInteraction);
  assert.equal(controller.getStats().trailPoints, 0);
  assert.equal(card.hasAttribute("data-ambient-magnetic"), false);
});
for (const removal of ["detached", "moved outside root"]) {
  test(`${removal} CTA releases pointer and magnet references before mutation discovery`, () => {
    const { root, clock, controller } = makeRuntime({ surfaceCount: 0 });
    const button = root.querySelector("#cta");
    pointer(button);
    clock.at(0);
    assert.equal(controller.inspectInteractionForTest().pointerButton, true);
    assert.equal(controller.inspectInteractionForTest().magnet, true);
    try {
      removal === "detached" ? button.remove() : document.body.append(button);
      clock.advance(34);
      assert.deepEqual(controller.inspectInteractionForTest(), clearedInteraction);
      assert.equal(button.hasAttribute("data-ambient-magnetic"), false);
    } finally {
      button.remove();
    }
  });
}
test("late IntersectionObserver callbacks after disposal cannot revive ticker or state", () => {
  const { root, clock, controller, reports } = makeRuntime();
  const observer = [...IntersectionStandIn.all].find((item) => item.targets.has(root));
  clock.at(0);
  controller.dispose();
  const disposedStats = controller.getStats();
  const registrations = clock.added;
  const reportCount = reports.length;
  // A retained callback models an already-queued delivery, even after disconnect.
  observer.emit(root, true);
  observer.emit(root, false);
  observer.emit(root, true);
  assert.equal(clock.listeners.size, 0);
  assert.equal(clock.added, registrations);
  assert.deepEqual(controller.getStats(), disposedStats);
  assert.equal(reports.length, reportCount);
  assert.equal(root.hasAttribute("data-ambient-running"), false);
  clock.advance(100);
  assert.deepEqual(controller.getStats(), disposedStats);
});
test("graph interaction freezes decoration frames until the final owner releases without changing quality or UI", () => {
  const { root, layer, clock, graphics, controller, reports } = makeRuntime();
  pointer(root.querySelector("#cta"));
  clock.at(0);
  const card = root.querySelector("#card-0");
  const light = card.querySelector(".ambient-card-edge-light");
  const aurora = root.querySelector(".ambient-aurora");
  const heading = root.querySelector("h1");
  const snapshot = {
    frames: controller.getStats().frames,
    clears: graphics.state.clears,
    fills: graphics.state.fills,
    edge: light.style.transform,
    aurora: aurora.style.transform,
    heading: heading.style.getPropertyValue("--ambient-title-position"),
    surfaces: controller.getStats().surfaces,
  };
  const first = graphOwner(), second = graphOwner();
  setGraphInteraction(first, true);
  assert.equal(controller.getStats().reason, "graph-interaction");
  assert.equal(layer.dataset.ambientReason, "graph-interaction");
  assert.equal(clock.listeners.size, 0);
  assert.deepEqual(controller.inspectInteractionForTest(), clearedInteraction);
  const reportsAfterPause = reports.length;
  setGraphInteraction(second, true);
  for (let frame = 0; frame < 100; frame++) {
    setGraphInteraction(first, true);
    clock.advance(34);
  }
  [...LongTaskStandIn.all][0].emit([120, 160, 200]);
  assert.equal(reports.length, reportsAfterPause);
  assert.equal(controller.getStats().frames, snapshot.frames);
  assert.equal(graphics.state.clears, snapshot.clears);
  assert.equal(graphics.state.fills, snapshot.fills);
  assert.equal(light.style.transform, snapshot.edge);
  assert.equal(aurora.style.transform, snapshot.aurora);
  assert.equal(heading.style.getPropertyValue("--ambient-title-position"), snapshot.heading);
  assert.equal(controller.getStats().surfaces, snapshot.surfaces);
  assert.equal(root.querySelector("#card-0"), card);
  assert.equal(root.dataset.ambientLevel, "rich");
  assert.equal(controller.getStats().degraded, false);
  setGraphInteraction(first, false);
  assert.equal(clock.listeners.size, 0);
  assert.equal(reports.length, reportsAfterPause);
  // A long interaction is not a slow rendering sample on resumption.
  clock.advance(60000);
  setGraphInteraction(second, false);
  assert.equal(controller.getStats().reason, "running");
  assert.equal(clock.listeners.size, 1);
  assert.equal(clock.added, 2);
  clock.advance(1);
  assert.equal(controller.getStats().frames, snapshot.frames + 1);
  assert.equal(controller.getStats().quality, "rich");
  assert.equal(controller.getStats().degraded, false);
});
test("an already-active graph token prevents any initial ambient ticker registration", () => {
  const owner = graphOwner();
  setGraphInteraction(owner, true);
  const { clock, controller } = makeRuntime();
  assert.equal(controller.getStats().reason, "graph-interaction");
  assert.equal(clock.added, 0);
  assert.equal(clock.listeners.size, 0);
  setGraphInteraction(owner, false);
  assert.equal(clock.added, 1);
  assert.equal(controller.getStats().running, true);
});
test("quiet remains higher priority than graph interaction start and end", () => {
  const owner = graphOwner();
  const { root, clock, controller } = makeRuntime({ level: "quiet" });
  setGraphInteraction(owner, true);
  assert.equal(controller.getStats().reason, "quiet");
  setGraphInteraction(owner, false);
  assert.equal(controller.getStats().reason, "quiet");
  assert.equal(root.dataset.ambientLevel, "quiet");
  assert.equal(clock.added, 0);
});
for (const reason of ["hidden", "blurred", "offscreen"]) {
  test(`${reason} and graph interaction must both clear before ambient resumes`, () => {
    const { root, clock, controller } = makeRuntime();
    const owner = graphOwner();
    const observer = [...IntersectionStandIn.all].find((item) => item.targets.has(root));
    const block = (active) => {
      if (reason === "hidden") {
        visibility = active ? "hidden" : "visible";
        document.dispatchEvent(new window.Event("visibilitychange"));
      } else if (reason === "blurred") {
        window.dispatchEvent(new window.Event(active ? "blur" : "focus"));
      } else observer.emit(root, !active);
    };
    setGraphInteraction(owner, true);
    block(true);
    assert.equal(controller.getStats().reason, reason);
    setGraphInteraction(owner, false);
    assert.equal(controller.getStats().reason, reason);
    assert.equal(clock.listeners.size, 0);
    block(false);
    assert.equal(controller.getStats().running, true);
    setGraphInteraction(owner, true);
    block(true);
    block(false);
    assert.equal(controller.getStats().reason, "graph-interaction");
    assert.equal(clock.listeners.size, 0);
    setGraphInteraction(owner, false);
    assert.equal(controller.getStats().running, true);
    assert.equal(clock.listeners.size, 1);
  });
}
test("disposing during interaction unsubscribes without releasing another component's token or reviving old runtime", () => {
  const owner = graphOwner();
  const first = makeRuntime({ surfaceCount: 0 });
  setGraphInteraction(owner, true);
  assert.equal(inspectGraphSubscriptionsForTest(), 1);
  first.controller.dispose();
  assert.equal(inspectGraphSubscriptionsForTest(), 0);
  assert.equal(isGraphInteracting(), true);
  const oldStats = first.controller.getStats();
  const registrations = first.clock.added;
  const reportCount = first.reports.length;
  const replacement = makeRuntime({ surfaceCount: 0 });
  assert.equal(inspectGraphSubscriptionsForTest(), 1);
  assert.equal(replacement.controller.getStats().reason, "graph-interaction");
  setGraphInteraction(owner, false);
  assert.equal(replacement.controller.getStats().running, true);
  assert.equal(first.clock.listeners.size, 0);
  assert.equal(first.clock.added, registrations);
  assert.equal(first.reports.length, reportCount);
  assert.deepEqual(first.controller.getStats(), oldStats);
  replacement.controller.dispose();
  assert.equal(inspectGraphSubscriptionsForTest(), 0);
});
test("twenty ambient create-interact-dispose cycles leave no shared subscriptions", () => {
  for (let cycle = 0; cycle < 20; cycle++) {
    const owner = graphOwner();
    const { controller, clock } = makeRuntime({ surfaceCount: 0 });
    setGraphInteraction(owner, true);
    assert.equal(clock.listeners.size, 0);
    controller.dispose();
    assert.equal(inspectGraphSubscriptionsForTest(), 0);
    setGraphInteraction(owner, false);
    assert.equal(clock.listeners.size, 0);
    assert.equal(controller.getStats().reason, "disposed");
  }
});
test("StrictMode honors system reduction during a graph interaction and retains the rich preference", async () => {
  function Harness() {
    const rootRef = React.useRef(null);
    return h("div", { ref: rootRef, className: "app-shell" },
      h(AmbientEffects, { rootRef, routeKey: "graph-priority" }),
      h("div", { className: "main-shell" }, h("h1", null, "交互测试")));
  }
  await mount(h(React.StrictMode, null, h(Harness)));
  assert.equal(inspectGraphSubscriptionsForTest(), 1);
  const layer = document.querySelector(".ambient-effects");
  const owner = graphOwner();
  await act(() => setGraphInteraction(owner, true));
  assert.equal(layer.dataset.ambientReason, "graph-interaction");
  await act(() => { reduced = true; mediaChanged(); });
  assert.equal(layer.dataset.ambientReason, "quiet");
  assert.equal(document.querySelector('[aria-label="界面动效档位"] select').value, "rich");
  await act(() => setGraphInteraction(owner, false));
  assert.equal(layer.dataset.ambientReason, "quiet");
  assert.equal(layer.dataset.ambientRunning, "false");
  await act(() => { reduced = false; mediaChanged(); });
  assert.equal(layer.dataset.ambientReason, "running");
  assert.equal(document.querySelector(".ambient-effects"), layer);
  assert.equal(inspectGraphSubscriptionsForTest(), 1);
});
test("parent root ref becomes ready after initial layout and StrictMode initializes with or without a portal host", async () => {
  function Harness({ host }) {
    const rootRef = React.useRef(null);
    return h(
      "div",
      { ref: rootRef, className: "app-shell" },
      h(AmbientEffects, { rootRef, routeKey: "test" }),
      h(
        "div",
        { className: "main-shell" },
        host && h("div", { className: "topbar-right" }),
        h("header", { className: "page-heading" }, h("h1", null, "标题")),
      ),
    );
  }
  await mount(h(React.StrictMode, null, h(Harness, { host: false })));
  let layer = document.querySelector(".ambient-effects");
  assert.equal(layer.dataset.ambientReady, "true");
  assert.equal(layer.closest(".app-shell").dataset.ambientLevel, "rich");
  await act(async () =>
    reactRoot.render(h(React.StrictMode, null, h(Harness, { host: true }))),
  );
  layer = document.querySelector(".ambient-effects");
  assert.equal(layer.dataset.ambientReady, "true");
  assert.ok(layer.closest(".app-shell").hasAttribute("data-ambient-shell"));
});
test("glyph-clipped flow keeps original text, skips long headings and restores at most two targets", async () => {
  const {root,clock,controller}=makeRuntime();
  const heading=root.querySelector('h1');
  assert.equal(heading.textContent,'测试标题');
  assert.equal(heading.childElementCount,0, 'there must be no cloned title or rectangular overlay');
  assert.equal(heading.hasAttribute('data-ambient-title-active'),true);
  const originalPosition=heading.style.getPropertyValue('--ambient-title-position');
  clock.at(0);clock.advance(100);
  assert.notEqual(heading.style.getPropertyValue('--ambient-title-position'),originalPosition);
  for (let i=0;i<240;i++) {
    clock.advance(40);
    const position=parseFloat(heading.style.getPropertyValue('--ambient-title-position'));
    assert.ok(position>=0 && position<=100, 'the opaque gradient must cover glyphs for the entire cycle');
  }
  const longHeading=document.createElement('h1');longHeading.textContent='长'.repeat(65);
  root.querySelector('.page-heading').append(longHeading);await wait(100);
  assert.equal(longHeading.hasAttribute('data-ambient-title-active'),false);
  for(const text of ['短标题二','短标题三']){const node=document.createElement('h1');node.textContent=text;root.querySelector('.page-heading').append(node);}
  await wait(100);assert.equal(root.querySelectorAll('[data-ambient-title-active]').length,2);
  const css=await readFile(new URL('./ambient-effects.css',import.meta.url),'utf8');
  assert.match(css,/background-clip:\s*text/);assert.doesNotMatch(css,/\.ambient-title-glint\s*\{/);
  controller.dispose();assert.equal(heading.textContent,'测试标题');
  assert.equal(heading.hasAttribute('data-ambient-title-active'),false);
  assert.equal(heading.style.getPropertyValue('--ambient-title-position'),'');
});
test("CSS defaults hide an uninitialized layer and preserves opaque reading surfaces", async () => {
  const css = await readFile(
    new URL("./ambient-effects.css", import.meta.url),
    "utf8",
  );
  assert.match(css, /\.ambient-effects\s*\{[^}]*visibility:\s*hidden/);
  assert.match(css, /data-ambient-ready="true"[^}]*visibility:\s*visible/);
  assert.match(css, /pointer-events:\s*none\s*!important/);
  assert.match(css, /\.document-paper[^}]*background-color:\s*#fff/);
  assert.equal(AMBIENT_LIMITS.particles, 72);
  assert.equal(AMBIENT_LIMITS.links, 110);
});

function HeadingHarness({show=true}={}) {
  const headingRef=React.useRef(null);
  return h('div',{className:'app-shell'},h('div',{className:'topbar-right'}),
    h('header',{className:'page-heading',ref:node=>{
      headingRef.current=node;
      if(node){Object.defineProperty(node,'clientWidth',{configurable:true,value:1000});Object.defineProperty(node,'clientHeight',{configurable:true,value:108});}
    }},h('h1',null,'文档中心'),show&&h(DocumentHeadingEffects,{headingRef})),
    h('section',{id:'protected-reading'},h('h1',null,'正文不可装饰'),h('p',null,'合成正文保持原样')),
    h(Pref,{name:'heading-pref'}));
}
test('document heading restores glyph flow and bounded light only inside its heading',async()=>{
  await mount(h(HeadingHarness));await wait(120);
  const title=document.querySelector('.page-heading h1'),layer=document.querySelector('.document-heading-effects');
  assert.equal(title.textContent,'文档中心');assert.equal(title.hasAttribute('data-ambient-title-active'),true);
  assert.equal(document.querySelector('#protected-reading h1').hasAttribute('data-ambient-title-active'),false);
  assert.equal(layer.parentElement.className,'page-heading');assert.equal(layer.getAttribute('aria-hidden'),'true');
  assert.equal(document.querySelectorAll('.topbar-right [aria-label="界面动效档位"]').length,2); // group + select
  const canvas=layer.querySelector('canvas');assert.ok(canvas.width*canvas.height<=600000);assert.equal(canvas.height,108);
  assert.equal(layer.dataset.running,'true');assert.ok(Number(layer.dataset.frames)>1);
});
test('document heading quiet and system reduce restore plain title and stop decoration',async()=>{
  await mount(h(HeadingHarness));await wait(80);
  await act(()=>seenPreferences.get('heading-pref').setLevel('quiet'));
  let layer=document.querySelector('.document-heading-effects');
  assert.equal(layer.dataset.running,'false');assert.equal(layer.dataset.motionLevel,'quiet');assert.equal(layer.querySelector('canvas').width,1);
  assert.equal(document.querySelector('.page-heading h1').hasAttribute('data-ambient-title-active'),false);
  await act(()=>seenPreferences.get('heading-pref').setLevel('rich'));
  await act(()=>{reduced=true;mediaChanged();});
  layer=document.querySelector('.document-heading-effects');assert.equal(layer.dataset.running,'false');assert.equal(layer.dataset.motionLevel,'quiet');
  assert.equal(document.querySelector('.page-heading h1').textContent,'文档中心');
});
test('document heading pauses on blur, visibility and offscreen then cleans late observers',async()=>{
  await mount(h(HeadingHarness));await wait(80);
  const layer=document.querySelector('.document-heading-effects'),title=document.querySelector('.page-heading h1');
  focused=false;window.dispatchEvent(new window.Event('blur'));assert.equal(layer.dataset.running,'false');
  const frames=layer.dataset.frames;await wait(100);assert.equal(layer.dataset.frames,frames);
  focused=true;window.dispatchEvent(new window.Event('focus'));assert.equal(layer.dataset.running,'true');
  visibility='hidden';document.dispatchEvent(new window.Event('visibilitychange'));assert.equal(layer.dataset.running,'false');
  visibility='visible';document.dispatchEvent(new window.Event('visibilitychange'));
  const observer=[...IntersectionStandIn.all][0],heading=title.parentElement;observer.emit(heading,false);assert.equal(layer.dataset.running,'false');
  observer.emit(heading,true);assert.equal(layer.dataset.running,'true');
  await act(()=>reactRoot.render(h(HeadingHarness,{show:false})));
  observer.emit(heading,true);assert.equal(layer.dataset.running,'false');assert.equal(title.hasAttribute('data-ambient-title-active'),false);
  assert.equal(gsap.getTweensOf(title).length,0);
});
test('document standard is shared across routes without zoom or whole-page scaling',async()=>{
  const css=await readFile(new URL('./app-shell.css',import.meta.url),'utf8');
  assert.match(css,/\.app-shell\s*\{[^}]*--sidebar:\s*216px/);
  assert.match(css,/--app-topbar-height:\s*64px/);assert.match(css,/--app-heading-size:\s*28px/);
  assert.match(css,/\.app-shell \.main-nav button[^}]*font-size:\s*14px/);
  assert.doesNotMatch(css,/\bzoom\s*:|transform\s*:\s*scale/);
  const local=await readFile(new URL('./document-center.css',import.meta.url),'utf8');
  assert.doesNotMatch(local,/data-page="documents"[^}]*--sidebar/);
});
