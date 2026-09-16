// Run: node --test src/motion.test.mjs (or npm test).
// Uses the application's pinned jsdom@26.1.0, never an npx/PATH fallback.
// No browser is launched. Native dialog/layout and missing CSS initial values
// are explicit DOM test stand-ins, not a browser rendering/matrix engine.
import test, { after, afterEach } from "node:test";
import assert from "node:assert/strict";
import Module, { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { build } from "esbuild";
const require = createRequire(import.meta.url);
const { JSDOM } = require("jsdom");
const dom = new JSDOM(
  '<!doctype html><html><body><button id="opener">Open</button><div id="root"></div></body></html>',
  { url: "http://localhost/", pretendToBeVisual: true },
);
const { window } = dom;
for (const key of [
  "window",
  "document",
  "HTMLElement",
  "HTMLDialogElement",
  "Element",
  "Node",
  "MutationObserver",
  "Event",
  "MouseEvent",
  "FocusEvent",
  "FormData",
])
  globalThis[key] = key === "window" ? window : window[key];
const nativeComputedStyle = window.getComputedStyle.bind(window);
// JSDOM 26 lacks computed initial values for the transform longhands. In the
// exit clone, translate is explicitly 'none', while scale/rotate otherwise read
// ''. GSAP combines these into the invalid 'rotate() scale()', then expects a
// browser-resolved matrix. Supply ONLY their standard 'none' initial values.
// Preserve all authored values and transform strings; do not substitute an
// identity matrix, patch GSAP, or suppress animation/runtime exceptions.
const transformInitialValues = new Set(['translate', 'scale', 'rotate']);
window.getComputedStyle = (element, pseudo) => {
  const computed = nativeComputedStyle(element, pseudo);
  const withInitialValue = (property, value) => transformInitialValues.has(property) && value === '' ? 'none' : value;
  return new Proxy(computed, {
    get(target, property) {
      if (property === 'getPropertyValue') return name => withInitialValue(name, target.getPropertyValue(name));
      const value = Reflect.get(target, property, target);
      return typeof value === 'function' ? value.bind(target) : withInitialValue(property, value);
    },
  });
};
globalThis.getComputedStyle = window.getComputedStyle;
const runtimeErrors = [];
window.addEventListener('error', event => {
  // Observe without preventDefault: JSDOM still reports errors normally, and
  // afterEach also fails explicitly if an animation callback throws.
  runtimeErrors.push(event.error ?? new Error(event.message));
});
globalThis.requestAnimationFrame = window.requestAnimationFrame.bind(window);
globalThis.cancelAnimationFrame = window.cancelAnimationFrame.bind(window);
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const queries = new Map();
let reduced = false;
window.matchMedia = (query) => {
  if (!queries.has(query)) {
    const target = new window.EventTarget();
    target.media = query;
    target.matches = query.includes("no-preference") ? !reduced : reduced;
    target.addListener = (listener) =>
      target.addEventListener("change", listener);
    target.removeListener = (listener) =>
      target.removeEventListener("change", listener);
    queries.set(query, target);
  }
  return queries.get(query);
};
function setReduced(value) {
  reduced = value;
  for (const [query, target] of queries) {
    target.matches = query.includes("no-preference") ? !value : value;
    target.dispatchEvent(new window.Event("change"));
  }
}
Object.defineProperty(window.HTMLElement.prototype, "inert", {
  configurable: true,
  get() {
    return this.hasAttribute("inert");
  },
  set(value) {
    this.toggleAttribute("inert", value);
  },
});
window.HTMLDialogElement.prototype.showModal = function () {
  this.open = true;
  this.querySelector("input, button")?.focus();
};
window.HTMLDialogElement.prototype.close = function () {
  this.open = false;
};
window.HTMLElement.prototype.getBoundingClientRect = function () {
  const top =
    this.tagName === "TR"
      ? 30 + Array.from(this.parentNode.children).indexOf(this) * 40
      : 30;
  return {
    x: 30,
    y: top,
    left: 30,
    top,
    width: 480,
    height: 36,
    right: 510,
    bottom: top + 36,
    toJSON() {},
  };
};
const React = require("react");
const { act } = React;
const { createRoot } = require("react-dom/client");
const gsap = require("gsap").gsap;
const { ScrollTrigger } = require("gsap/dist/ScrollTrigger");
window.scrollTo = () => {}; // jsdom has no native scrolling; tests set measured rectangles explicitly.
const compiled = await build({
  stdin: { contents: 'export * from "./ui"; export { useMotionPreferences } from "./motionPreferences";',
    resolveDir: resolve("src"), loader: "tsx" },
  bundle: true,
  platform: "node",
  format: "cjs",
  external: [
    "react",
    "react-dom",
    "react-dom/*",
    "react/jsx-runtime",
    "gsap",
    "gsap/ScrollTrigger",
    "@gsap/react",
  ],
  write: false,
  logLevel: "silent",
});
const loaded = new Module(resolve("src/motion-test-runtime.cjs"));
loaded.filename = resolve("src/motion-test-runtime.cjs");
loaded.paths = Module._nodeModulePaths(dirname(loaded.filename));
// Match Vite's ESM default import while keeping the exact installed GSAP engine.
const runtimeRequire = loaded.require.bind(loaded);
loaded.require = (id) => id === "gsap" ? gsap : id === "gsap/ScrollTrigger" ? { ScrollTrigger } : runtimeRequire(id);
loaded._compile(compiled.outputFiles[0].text, loaded.filename);
const { Modal, FormModal, Motion, MotionItem, useMotionPreferences } = loaded.exports;
const h = React.createElement;
let preferences;
function PreferencesProbe() { preferences = useMotionPreferences(); return null; }
const withPreferences = child => h(React.Fragment, null, h(PreferencesProbe), child);
let root;
const wait = (milliseconds) =>
  act(() => new Promise((done) => setTimeout(done, milliseconds)));
async function mount(element) {
  root = createRoot(document.getElementById("root"));
  await act(() => root.render(element));
}
const click = async (element) =>
  act(() =>
    element.dispatchEvent(
      new window.MouseEvent("click", { bubbles: true, cancelable: true }),
    ),
  );
const tweenTargets = () =>
  gsap.globalTimeline
    .getChildren(true, true, false)
    .flatMap((tween) => tween.targets());
afterEach(async () => {
  if (root) {
    await act(() => root.unmount());
    root = undefined;
  }
  await wait(330);
  if (preferences) await act(() => preferences.setLevel('rich'));
  setReduced(false);
  window.localStorage.removeItem('fkb:ui:motion-level');
  window.dispatchEvent(new window.StorageEvent('storage', { key: 'fkb:ui:motion-level', newValue: null }));
  await wait(20);
  assert.equal(
    document.querySelectorAll("dialog[open], .kb-motion-exit").length,
    0,
  );
  assert.deepEqual(runtimeErrors.splice(0).map(error => error.message), [], 'animation callbacks must not throw');
  preferences = undefined;
});
after(() => {
  // The isolated test process owns this entire plugin instance. Components must
  // have cleaned their own triggers before stopping the closed jsdom engine.
  assert.equal(ScrollTrigger.getAll().length, 0);
  ScrollTrigger.disable();
  gsap.ticker.sleep();
  window.close();
});

test("page stagger is capped at six rows and never scans/animates document paragraphs", async () => {
  await mount(
    h(
      Motion,
      { className: "page-root" },
      h(
        "div",
        { className: "table-scroll" },
        h(
          "table",
          { className: "plain-table" },
          h(
            "tbody",
            null,
            ...Array.from({ length: 100 }, (_, i) =>
              h("tr", { key: i }, h("td", null, String(i))),
            ),
          ),
        ),
      ),
      ...Array.from({ length: 100 }, (_, i) =>
        h("p", { key: "p" + i }, "段落 " + i),
      ),
    ),
  );
  const rows = new Set(tweenTargets().filter((node) => node.tagName === "TR"));
  assert.equal(rows.size, 6);
  assert.equal(
    tweenTargets().some((node) => node.tagName === "P"),
    false,
  );
  await wait(440);
  assert.equal(document.querySelector("tr").style.opacity, "");
});
test("paragraph motion is opt-in and typing does not restart a stable identity", async () => {
  const render = (text) =>
    h(
      "div",
      null,
      ...Array.from({ length: 100 }, (_, i) =>
        h(
          MotionItem,
          { key: i, identity: i, active: i === 2, className: "paragraph-item" },
          i === 2 ? text : "静态正文",
        ),
      ),
    );
  await mount(render("输入前"));
  assert.equal(
    new Set(
      tweenTargets().filter((node) =>
        node.classList?.contains("paragraph-item"),
      ),
    ).size,
    1,
  );
  await wait(360);
  await act(() => root.render(render("输入后")));
  assert.equal(
    tweenTargets().some((node) => node.classList?.contains("paragraph-item")),
    false,
  );
});
function ModalHarness({ mode = "close", busy = false, onClosed }) {
  const [open, setOpen] = React.useState(true);
  const close = () => {
    onClosed();
    if (mode !== "veto") setOpen(false);
  };
  return open
    ? h(
        Modal,
        { title: "测试", close, busy },
        h("input", { defaultValue: "内容" }),
        h(
          "footer",
          { className: "modal-foot" },
          h("button", { onClick: close }, "取消"),
        ),
      )
    : null;
}
test("modal keeps native dialog mounted until exit completes, then restores opener focus", async () => {
  let closed = 0;
  document.getElementById("opener").focus();
  await mount(h(ModalHarness, { onClosed: () => closed++ }));
  const dialog = document.querySelector("dialog");
  await click(dialog.querySelector('[aria-label="关闭弹窗"]'));
  assert.equal(closed, 0);
  assert.equal(dialog.open, true);
  assert.equal(dialog.inert, true);
  await wait(360);
  assert.equal(closed, 1);
  assert.equal(document.querySelector("dialog"), null);
  assert.equal(document.activeElement.id, "opener");
});
test("reduced-motion closes synchronously without an exit delay", async () => {
  setReduced(true);
  let closed = 0;
  await mount(h(ModalHarness, { onClosed: () => closed++ }));
  await click(document.querySelector('[aria-label="关闭弹窗"]'));
  assert.equal(closed, 1);
  assert.equal(document.querySelector("dialog"), null);
});
test("repeated Escape requests result in one actual close callback", async () => {
  let closed = 0;
  await mount(h(ModalHarness, { onClosed: () => closed++ }));
  const dialog = document.querySelector("dialog");
  await act(() => {
    for (let i = 0; i < 3; i++)
      dialog.dispatchEvent(
        new window.Event("cancel", { bubbles: true, cancelable: true }),
      );
  });
  assert.equal(closed, 0);
  await wait(360);
  assert.equal(closed, 1);
});
test("busy modal refuses Escape and footer close", async () => {
  let closed = 0;
  await mount(h(ModalHarness, { busy: true, onClosed: () => closed++ }));
  await act(() =>
    document
      .querySelector("dialog")
      .dispatchEvent(
        new window.Event("cancel", { bubbles: true, cancelable: true }),
      ),
  );
  await click(document.querySelector("footer button"));
  await wait(440);
  assert.equal(closed, 0);
  assert.equal(document.querySelector("dialog").open, true);
});
test("a parent veto restores the dialog instead of leaving an invisible blocking backdrop", async () => {
  let attempted = 0;
  await mount(h(ModalHarness, { mode: "veto", onClosed: () => attempted++ }));
  await click(document.querySelector('[aria-label="关闭弹窗"]'));
  await wait(370);
  const dialog = document.querySelector("dialog");
  assert.equal(attempted, 1);
  assert.equal(dialog.open, true);
  assert.equal(dialog.inert, false);
  assert.equal(dialog.dataset.kbModal, "open");
  assert.equal(dialog.style.opacity, "");
});
test("plain footer callback is wrapped without delaying unrelated actions", async () => {
  let closed = 0;
  await mount(h(ModalHarness, { onClosed: () => closed++ }));
  await click(document.querySelector("footer button"));
  assert.equal(closed, 0);
  await wait(360);
  assert.equal(closed, 1);
});
test("FormModal submits immediately and closes after the successful-save exit", async () => {
  let saves = 0;
  let closed = 0;
  function FormHarness() {
    const [open, setOpen] = React.useState(true);
    return open
      ? h(
          FormModal,
          {
            title: "保存",
            close: () => {
              closed++;
              setOpen(false);
            },
            submit: async () => {
              saves++;
            },
          },
          h("input", { name: "title", defaultValue: "知识" }),
        )
      : null;
  }
  await mount(h(FormHarness));
  await act(() =>
    document
      .querySelector("form")
      .dispatchEvent(
        new window.Event("submit", { bubbles: true, cancelable: true }),
      ),
  );
  assert.equal(saves, 1);
  assert.equal(closed, 0);
  await wait(360);
  assert.equal(closed, 1);
});
test("selection-bar exit copy is inert, bounded and skipped for reduced motion", async () => {
  const probe = document.createElement('div');
  Object.assign(probe.style, { transform: 'none', translate: 'none' });
  document.body.append(probe);
  assert.equal(nativeComputedStyle(probe).scale, '');
  assert.equal(nativeComputedStyle(probe).rotate, '');
  assert.equal(getComputedStyle(probe).scale, 'none');
  assert.equal(getComputedStyle(probe).getPropertyValue('rotate'), 'none');
  probe.style.scale = '1.25'; probe.style.rotate = '5deg';
  probe.style.transform = 'matrix(1, 0, 0, 1, 12, 8)';
  assert.equal(getComputedStyle(probe).scale, '1.25');
  assert.equal(getComputedStyle(probe).rotate, '5deg');
  assert.equal(getComputedStyle(probe).transform, 'matrix(1, 0, 0, 1, 12, 8)');
  probe.remove();
  await mount(
    h(
      Motion,
      { className: "selection-bar" },
      h("button", { id: "selected-action" }, "批量操作"),
    ),
  );
  await wait(350);
  await act(() => root.render(null));
  const copy = document.querySelector(".kb-motion-exit");
  assert.ok(copy);
  assert.equal(copy.inert, true);
  assert.equal(copy.getAttribute("aria-hidden"), "true");
  assert.equal(copy.querySelector("[id]"), null);
  await wait(330);
  assert.equal(document.querySelector(".kb-motion-exit"), null);
  setReduced(true);
  await act(() =>
    root.render(h(Motion, { className: "selection-bar" }, "已选")),
  );
  await act(() => root.render(null));
  assert.equal(document.querySelector(".kb-motion-exit"), null);
});
test("Toast is detected only inside the mounted application root and gets a safe exit", async () => {
  const render = (show) =>
    h(
      React.Fragment,
      null,
      h(Motion, { className: "page-root" }, h("p", null, "页面")),
      show && h("div", { className: "toast", role: "status" }, "保存完成"),
    );
  await mount(render(true));
  await wait(360);
  await act(() => root.render(render(false)));
  assert.equal(
    document.querySelector(".kb-motion-exit")?.getAttribute("aria-live"),
    null,
  );
  assert.ok(document.querySelector(".kb-motion-exit"));
  await wait(330);
  assert.equal(document.querySelector(".kb-motion-exit"), null);
});
test("StrictMode cleanup does not produce phantom exit copies during its replay", async () => {
  await mount(
    h(
      React.StrictMode,
      null,
      h(Motion, { className: "selection-bar" }, "选择操作"),
    ),
  );
  assert.equal(document.querySelector(".kb-motion-exit"), null);
  await wait(440);
  assert.equal(document.querySelector(".selection-bar").style.opacity, "");
});
test("switching to reduced motion during modal exit completes close immediately", async () => {
  let closed = 0;
  await mount(h(ModalHarness, { onClosed: () => closed++ }));
  await click(document.querySelector('[aria-label="关闭弹窗"]'));
  assert.equal(closed, 0);
  await act(() => setReduced(true));
  assert.equal(closed, 1);
  assert.equal(document.querySelector("dialog"), null);
});
test("external modal unmount cleans animation and top-layer state without invoking close twice", async () => {
  let closed = 0;
  document.getElementById("opener").focus();
  await mount(
    h(React.StrictMode, null, h(ModalHarness, { onClosed: () => closed++ })),
  );
  await click(document.querySelector('[aria-label="关闭弹窗"]'));
  await act(() => root.render(null));
  await wait(450);
  assert.equal(closed, 0);
  assert.equal(document.querySelector("dialog"), null);
  assert.equal(document.activeElement.id, "opener");
});

test("page transition is a labelled real GSAP timeline and removes old content on route change", async () => {
  const render = (id) => h(Motion, { key: id, identity: id, className: 'page-root' },
    h('header', { className: 'page-heading' }, h('div', null, id)),
    h('nav', { className: 'tabs' }, '导航'), h('section', { 'data-motion-reveal': true }, '内容'));
  await mount(render('旧页面'));
  const first = gsap.globalTimeline.getChildren(false, false, true).find(t => t.data === 'kb-motion:page');
  assert.ok(first instanceof gsap.core.Timeline);
  assert.deepEqual(Object.keys(first.labels), ['surface', 'heading', 'content']);
  assert.ok(first.getChildren().length >= 3);
  const old = document.querySelector('[data-kb-motion="page"]');
  await act(() => root.render(render('新页面')));
  assert.equal(old.isConnected, false);
  assert.equal(document.body.textContent.includes('旧页面'), false);
  assert.equal(document.querySelectorAll('.kb-motion-exit').length, 0);
});

test("overview cascades bounded sections and excludes its editable region and fields", async () => {
  await mount(h(Motion, { className: 'resource-overview', identity: 'resource-a' },
    h('header', null, '概要'), h('div', { className: 'overview-name' }, '名称'), h('dl', null, h('dt', null, '日期')),
    h('section', { className: 'input-section' }, h('input', { defaultValue: '编辑中' })),
    h('div', { contentEditable: true, suppressContentEditableWarning: true }, h('p', null, '连续文稿'))));
  const timeline = gsap.globalTimeline.getChildren(false, false, true).find(t => t.data === 'kb-motion:overview');
  assert.ok(timeline instanceof gsap.core.Timeline);
  const targets = timeline.getChildren().flatMap(tween => tween.targets());
  assert.equal(targets.length, 3);
  assert.equal(targets.some(node => node.matches('input, p, [contenteditable], .input-section, .resource-overview')), false);
});

test("off-screen surface gets an owned ScrollTrigger and enters on visibility without touching prose", async () => {
  const original = window.HTMLElement.prototype.getBoundingClientRect;
  let below = true;
  window.HTMLElement.prototype.getBoundingClientRect = function () {
    const rect = original.call(this);
    return this.dataset?.deferred && below ? { ...rect, top: 1400, y: 1400, bottom: 1436 } : rect;
  };
  try {
    await mount(h(Motion, { className: 'page-root' },
      h('section', { 'data-motion-reveal': true, 'data-deferred': true }, '待进入摘要'),
      h('div', { contentEditable: true, suppressContentEditableWarning: true },
        h('p', { 'data-motion-reveal': true }, '不动画正文'))));
    const surface = document.querySelector('[data-deferred]');
    const triggers = ScrollTrigger.getAll().filter(t => t.trigger === surface);
    assert.equal(triggers.length, 1);
    assert.equal(surface.style.opacity, '', 'off-screen content is never hidden ahead of time');
    assert.equal(triggers[0].vars.once, true);
    assert.equal(triggers[0].vars.pin, undefined);
    below = false;
    await act(() => { triggers[0].refresh(); triggers[0].update(); });
    // Crossing a synthetic layout boundary can lie exactly at start in jsdom;
    // invoke its registered native callback to verify scoped entrance semantics.
    await act(() => triggers[0].vars.onEnter(triggers[0]));
    assert.ok(tweenTargets().includes(surface));
    assert.equal(tweenTargets().some(node => node.tagName === 'P'), false);
    await act(() => root.render(null));
    assert.equal(ScrollTrigger.getAll().filter(t => t.trigger === surface).length, 0);
  } finally {
    window.HTMLElement.prototype.getBoundingClientRect = original;
  }
});

test("quiet preference interrupts only owned motion and keeps focused input intact", async () => {
  await mount(h(Motion, { className: 'page-root' },
    h('header', { className: 'page-heading' }, h('div', null, '标题')),
    h('input', { defaultValue: '草稿' })));
  const input = document.querySelector('#root input');
  input.focus();
  input.setSelectionRange(1, 2);
  const unrelated = document.createElement('div');
  document.body.append(unrelated);
  const foreignTween = gsap.to(unrelated, { x: 20, duration: 2 });
  try {
    await act(() => {
      window.localStorage.setItem('fkb:ui:motion-level', 'quiet');
      window.dispatchEvent(new window.StorageEvent('storage', { key: 'fkb:ui:motion-level', newValue: 'quiet' }));
    });
    assert.equal(document.querySelector('[data-kb-motion]').dataset.kbMotionLevel, 'quiet');
    assert.equal(document.activeElement, input);
    assert.equal(input.value, '草稿');
    assert.equal(input.selectionStart, 1);
    assert.equal(tweenTargets().some(node => node instanceof Node && document.getElementById('root').contains(node)), false);
    assert.ok(foreignTween.parent, 'another component animation must remain registered');
  } finally { foreignTween.revert(); unrelated.remove(); }
});

test("application quiet opens a modal without tween and closes synchronously with opener focus", async () => {
  window.localStorage.setItem('fkb:ui:motion-level', 'quiet');
  document.getElementById('opener').focus();
  let closed = 0;
  await mount(withPreferences(h(ModalHarness, { onClosed: () => closed++ })));
  assert.equal(preferences.effective, 'quiet');
  const dialog = document.querySelector('dialog');
  assert.equal(dialog.dataset.kbModal, 'open');
  assert.equal(gsap.isTweening(dialog), false);
  assert.equal(dialog.style.opacity, '');
  await click(dialog.querySelector('[aria-label="关闭弹窗"]'));
  assert.equal(closed, 1);
  assert.equal(document.querySelector('dialog'), null);
  assert.equal(document.activeElement.id, 'opener');
});

test("switching shared preference to quiet during entry finishes the same modal without remount", async () => {
  let closed = 0;
  await mount(withPreferences(h(ModalHarness, { onClosed: () => closed++ })));
  const dialog = document.querySelector('dialog');
  const focused = document.activeElement;
  assert.equal(dialog.dataset.kbModal, 'opening');
  const foreign = document.createElement('div'); document.body.append(foreign);
  const unrelated = gsap.to(foreign, { x: 30, duration: 2 });
  try {
    await act(() => preferences.setLevel('quiet'));
    assert.equal(document.querySelector('dialog'), dialog);
    assert.equal(dialog.dataset.kbModal, 'open');
    assert.equal(dialog.inert, false);
    assert.equal(dialog.style.transform, '');
    assert.equal(dialog.style.opacity, '');
    assert.equal(gsap.isTweening(dialog), false);
    assert.equal(document.activeElement, focused);
    assert.equal(closed, 0);
    assert.ok(unrelated.parent);
    await act(() => preferences.setLevel('rich'));
    assert.equal(document.querySelector('dialog'), dialog);
    assert.equal(dialog.dataset.kbModal, 'open');
    assert.equal(gsap.isTweening(dialog), false, 'enabling motion must not replay native showModal or entrance');
  } finally { unrelated.revert(); foreign.remove(); }
});

test("quiet during modal exit completes one close immediately and cancels later callbacks", async () => {
  let closed = 0;
  document.getElementById('opener').focus();
  await mount(withPreferences(h(ModalHarness, { onClosed: () => closed++ })));
  const dialog = document.querySelector('dialog');
  await click(dialog.querySelector('[aria-label="关闭弹窗"]'));
  assert.equal(closed, 0);
  assert.equal(dialog.inert, true);
  await act(() => preferences.setLevel('quiet'));
  assert.equal(closed, 1);
  assert.equal(document.querySelector('dialog'), null);
  assert.equal(document.activeElement.id, 'opener');
  await wait(460);
  assert.equal(closed, 1, 'watchdog/exit callback must not close a second time');
});

test("quiet retains busy protection including a busy change during an already-running exit", async () => {
  let closed = 0;
  const render = busy => withPreferences(h(ModalHarness, { busy, onClosed: () => closed++ }));
  await mount(render(false));
  const dialog = document.querySelector('dialog');
  const input = dialog.querySelector('input');
  input.focus(); input.setSelectionRange(0, 1);
  await click(dialog.querySelector('[aria-label="关闭弹窗"]'));
  await act(() => { root.render(render(true)); preferences.setLevel('quiet'); });
  assert.equal(closed, 0);
  assert.equal(document.querySelector('dialog'), dialog);
  assert.equal(dialog.inert, false);
  assert.equal(dialog.dataset.kbModal, 'open');
  assert.equal(document.activeElement, input);
  assert.equal(input.selectionStart, 0);
  assert.equal(input.selectionEnd, 1);
  await act(() => dialog.dispatchEvent(new window.Event('cancel', { cancelable: true })));
  await click(dialog.querySelector('footer button'));
  assert.equal(closed, 0);
  await act(() => root.render(render(false)));
  await click(dialog.querySelector('footer button'));
  assert.equal(closed, 1);
  assert.equal(document.querySelector('dialog'), null);
});

test("quiet during a vetoed exit restores the live dialog and preserves its draft and focus", async () => {
  let attempted = 0;
  await mount(withPreferences(h(ModalHarness, { mode: 'veto', onClosed: () => attempted++ })));
  const dialog = document.querySelector('dialog');
  const input = dialog.querySelector('input');
  input.value = '未保存文稿'; input.focus(); input.setSelectionRange(1, 3);
  await click(dialog.querySelector('[aria-label="关闭弹窗"]'));
  await act(() => preferences.setLevel('quiet'));
  assert.equal(attempted, 1);
  await wait(20); // Existing parent-veto recovery is next-task, not an exit animation delay.
  assert.equal(document.querySelector('dialog'), dialog);
  assert.equal(dialog.open, true);
  assert.equal(dialog.inert, false);
  assert.equal(dialog.dataset.kbModal, 'open');
  assert.equal(dialog.hasAttribute('aria-busy'), false);
  assert.equal(input.value, '未保存文稿');
  assert.equal(document.activeElement, input);
  assert.equal(input.selectionStart, 1);
  assert.equal(input.selectionEnd, 3);
  await wait(440);
  assert.equal(attempted, 1);
});
