// Real React/GSAP/Flip, with explicit jsdom rectangles (not a browser layout engine).
import assert from 'node:assert/strict';
import test, { after, afterEach } from 'node:test';
import Module, { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { build } from 'esbuild';

const require = createRequire(import.meta.url);
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  url: 'http://localhost/', pretendToBeVisual: true,
});
const { window } = dom;
for (const name of ['window', 'document', 'HTMLElement', 'Element', 'Node', 'Event', 'MutationObserver']) {
  globalThis[name] = name === 'window' ? window : window[name];
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
globalThis.requestAnimationFrame = window.requestAnimationFrame.bind(window);
globalThis.cancelAnimationFrame = window.cancelAnimationFrame.bind(window);
const nativeComputed = window.getComputedStyle.bind(window);
const transformLonghands = new Set(['translate', 'rotate', 'scale']);
window.getComputedStyle = (node) => new Proxy(nativeComputed(node), {
  get(target, property) {
    if (property === 'getPropertyValue') return name => {
      const value = target.getPropertyValue(name);
      return value === '' && transformLonghands.has(name) ? 'none' : value;
    };
    const value = Reflect.get(target, property, target);
    if (typeof value === 'function') return value.bind(target);
    return value === '' && transformLonghands.has(property) ? 'none' : value;
  },
});
globalThis.getComputedStyle = window.getComputedStyle;
let reduced = false;
const queries = new Map();
window.matchMedia = query => {
  if (!queries.has(query)) {
    const media = new window.EventTarget();
    media.media = query;
    media.matches = query.includes('no-preference') ? !reduced : reduced;
    queries.set(query, media);
  }
  return queries.get(query);
};
function setReduced(value) {
  reduced = value;
  for (const [query, media] of queries) {
    media.matches = query.includes('no-preference') ? !value : value;
    media.dispatchEvent(new window.Event('change'));
  }
}
function setLevel(value) {
  window.localStorage.setItem('fkb:ui:motion-level', value);
  window.dispatchEvent(new window.StorageEvent('storage', { key: 'fkb:ui:motion-level', newValue: value }));
}
let scrollOffset = 0;
const defaultLayout = node => {
  const index = node.dataset.flipId ? Number(node.dataset.layoutIndex || 0) : 0;
  const top = node.dataset.offscreen ? 3000 : 20 + index * 10 - (node.dataset.flipId ? scrollOffset : 0);
  return { x: 20, y: top, left: 20, top, width: 320, height: 8, right: 340, bottom: top + 8, toJSON() {} };
};
let layout = defaultLayout;
const measured = [];
window.HTMLElement.prototype.getBoundingClientRect = function () { measured.push(this); return layout(this); };
const intersectionObservers = new Set();
// JSDOM has no native layout/IntersectionObserver. Supply browser-calculated
// intersections from the layout fixture WITHOUT calling JS getBoundingClientRect;
// measurement-count assertions below count the real hook + real Flip calls only.
class TestIntersectionObserver {
  constructor(callback) { this.callback = callback; this.nodes = new Map(); this.disposed = false; this.pending = false;
    intersectionObservers.add(this); }
  observe(node) { this.nodes.set(node, undefined); this.queue(); }
  unobserve(node) { this.nodes.delete(node); }
  disconnect() { this.disposed = true; this.nodes.clear(); intersectionObservers.delete(this); }
  takeRecords() { return []; }
  queue() {
    if (this.pending || this.disposed) return;
    this.pending = true;
    queueMicrotask(() => { this.pending = false; this.flush(); });
  }
  flush() {
    if (this.disposed) return;
    const entries = [];
    for (const [node, prior] of this.nodes) {
      const box = layout(node);
      let left = Math.max(0, box.left), top = Math.max(0, box.top);
      let right = Math.min(window.innerWidth, box.right), bottom = Math.min(window.innerHeight, box.bottom);
      for (let parent = node.parentElement; parent; parent = parent.parentElement) {
        if (/(auto|scroll|hidden|clip)/.test(parent.style.overflow + parent.style.overflowY + parent.style.overflowX)) {
          const clip = layout(parent);
          left = Math.max(left, clip.left); right = Math.min(right, clip.right);
          top = Math.max(top, clip.top); bottom = Math.min(bottom, clip.bottom);
        }
      }
      const isIntersecting = node.isConnected && right > left && bottom > top;
      if (isIntersecting !== prior) entries.push({ target: node, isIntersecting,
        intersectionRect: { left, top, right, bottom, width: Math.max(0, right - left), height: Math.max(0, bottom - top) } });
      this.nodes.set(node, isIntersecting);
    }
    if (entries.length) this.callback(entries, this);
  }
}
window.IntersectionObserver = TestIntersectionObserver;
const errors = [];
window.addEventListener('error', event => errors.push(event.error ?? new Error(event.message)));
const React = require('react');
const { act } = React;
const { createRoot } = require('react-dom/client');
const gsap = require('gsap').gsap;
const { Flip } = require('gsap/dist/Flip');
const compiled = await build({
  stdin: { contents: 'export { useListFlip } from "./useListFlip";', resolveDir: resolve('src'), loader: 'ts' },
  bundle: true, platform: 'node', format: 'cjs', write: false, logLevel: 'silent',
  external: ['react', 'react/jsx-runtime', 'gsap', 'gsap/Flip', '@gsap/react'],
});
const loaded = new Module(resolve('src/list-flip-test-runtime.cjs'));
loaded.filename = resolve('src/list-flip-test-runtime.cjs');
loaded.paths = Module._nodeModulePaths(dirname(loaded.filename));
const runtimeRequire = loaded.require.bind(loaded);
loaded.require = id => id === 'gsap' ? gsap : id === 'gsap/Flip' ? { Flip } : runtimeRequire(id);
loaded._compile(compiled.outputFiles[0].text, loaded.filename);
const { useListFlip } = loaded.exports;
const originalFrom = Flip.from;
const calls = [];
Flip.from = (state, vars) => {
  const call = {
    old: state.elementStates.map(item => ({ id: item.element.dataset.flipId, top: item.bounds.top })),
    targets: [...vars.targets], vars,
  };
  call.animation = originalFrom(state, vars);
  calls.push(call);
  return call.animation;
};
const h = React.createElement;
let root;
const settle = () => act(() => new Promise(resolve => setTimeout(resolve, 550)));
async function mount(element) {
  root = createRoot(document.getElementById('root'));
  await act(() => root.render(element));
  await act(() => new Promise(resolve => window.requestAnimationFrame(resolve)));
}
function List({ ids, identity = ids.join('|'), editor = false, extra = false, view = 'list', text = '标题', offscreen = [] }) {
  const ref = React.useRef(null);
  useListFlip(ref, identity + ':' + view, '[data-flip-id]');
  return h('div', { ref, className: 'list-root' },
    view === 'list' && ids.map((id, index) => h('div', {
      key: id + ':' + index * Number(extra), 'data-flip-id': id,
      'data-layout-index': index,
      'data-offscreen': offscreen.includes(id) ? true : undefined,
    }, editor && index === 0 ? h('textarea', { defaultValue: '文稿' }) : text + id)),
    view === 'graph' && h('canvas', { 'aria-label': '图谱' }));
}
afterEach(async () => {
  if (root) { await act(() => root.unmount()); root = undefined; }
  await act(() => { setReduced(false); setLevel('rich'); });
  assert.deepEqual(errors.splice(0).map(error => error.message), []);
  assert.equal(document.querySelectorAll('[data-flip-id]').length, 0);
  assert.equal(gsap.globalTimeline.getChildren(true, false, true).filter(t => t.data === 'isFlip').length, 0);
  assert.equal(intersectionObservers.size, 0, 'owned observers must disconnect after unmount/StrictMode');
  calls.length = 0;
  measured.length = 0;
  scrollOffset = 0;
  layout = defaultLayout;
});
after(() => { Flip.from = originalFrom; gsap.ticker.sleep(); window.close(); });

test('FLIP captures the previous committed positions before a real keyed DOM reorder', async () => {
  await mount(h(List, { ids: ['a', 'b', 'c'] }));
  assert.equal(calls.length, 0);
  const a = document.querySelector('[data-flip-id="a"]');
  await act(() => root.render(h(List, { ids: ['c', 'a', 'b'] })));
  assert.equal(calls.length, 1);
  assert.ok(calls[0].animation instanceof gsap.core.Timeline);
  assert.equal(calls[0].old.find(item => item.id === 'a').top, 20);
  assert.equal(a.getBoundingClientRect().top, 30);
  assert.deepEqual([...document.querySelectorAll('[data-flip-id]')].map(node => node.dataset.flipId), ['c', 'a', 'b']);
  assert.equal(calls[0].vars.absolute, false);
  assert.equal(calls[0].vars.absoluteOnLeave, false);
  assert.equal(calls[0].vars.onLeave, undefined);
  await settle();
  assert.equal(a.style.transform, '', 'Flip restores its inline transforms on completion');
});

test('removed or replaced elements are never animated or reinserted, and graph switch clears old state', async () => {
  await mount(h(List, { ids: ['a', 'private', 'c'] }));
  const removed = document.querySelector('[data-flip-id="private"]');
  await act(() => root.render(h(List, { ids: ['c', 'a'] })));
  assert.equal(removed.isConnected, false);
  assert.equal(calls[0].targets.includes(removed), false);
  assert.equal(calls[0].old.some(item => item.id === 'private'), false);
  await act(() => root.render(h(List, { ids: ['c', 'a'], view: 'graph' })));
  assert.equal(document.querySelectorAll('[data-flip-id]').length, 0);
  const before = calls.length;
  await act(() => root.render(h(List, { ids: ['c', 'a'] })));
  assert.equal(calls.length, before, 'new list DOM must not animate from revoked/stale graph-era nodes');
});

test('only visible scoped IDs are animated, capped at forty and excluding the document editor', async () => {
  const ids = Array.from({ length: 80 }, (_, index) => 'item-' + index);
  await mount(h(List, { ids, editor: true, offscreen: ['item-1'] }));
  await act(() => root.render(h(List, { ids: [...ids].reverse(), editor: true, offscreen: ['item-1'] })));
  assert.ok(calls.length);
  assert.ok(calls[0].targets.length <= 40);
  assert.equal(calls[0].targets.some(node => node.dataset.offscreen || node.querySelector('textarea')), false);
});

test('unchanged identity from typing/selection does not restart FLIP', async () => {
  await mount(h(List, { ids: ['a', 'b'], text: '输入前' }));
  await act(() => root.render(h(List, { ids: ['a', 'b'], text: '输入后' })));
  assert.equal(calls.length, 0);
});

test('quiet and system reduced-motion display reordered content with no FLIP', async () => {
  await act(() => setLevel('quiet'));
  await mount(h(List, { ids: ['a', 'b'] }));
  await act(() => root.render(h(List, { ids: ['b', 'a'] })));
  assert.equal(calls.length, 0);
  await act(() => { setReduced(true); setLevel('rich'); });
  await act(() => root.render(h(List, { ids: ['a', 'b'] })));
  assert.equal(calls.length, 0);
  assert.equal(document.querySelector('[data-flip-id="a"]').style.transform, '');
});

test('switching quiet during a reorder restores only owned styles and preserves other tweens', async () => {
  await mount(h(List, { ids: ['a', 'b'] }));
  await act(() => root.render(h(List, { ids: ['b', 'a'] })));
  const foreign = document.createElement('div'); document.body.append(foreign);
  const tween = gsap.to(foreign, { x: 50, duration: 2 });
  try {
    await act(() => setLevel('quiet'));
    for (const node of document.querySelectorAll('[data-flip-id]')) assert.equal(node.style.transform, '');
    assert.ok(tween.parent);
  } finally { tween.revert(); foreign.remove(); }
});

test('StrictMode replay and rapid replacement clean the previous timeline without phantom DOM', async () => {
  const render = ids => h(React.StrictMode, null, h(List, { ids }));
  await mount(render(['a', 'b', 'c']));
  assert.equal(calls.length, 0);
  await act(() => root.render(render(['b', 'c', 'a'])));
  const first = calls.at(-1).animation;
  await act(() => root.render(render(['c', 'a', 'b'])));
  assert.equal(first.parent, null);
  assert.equal(document.querySelectorAll('[data-flip-id]').length, 3);
  await settle();
  assert.equal(document.body.textContent.includes('private'), false);
});

test('duplicate IDs are excluded rather than matching the wrong record', async () => {
  await mount(h(List, { ids: ['duplicate', 'duplicate', 'unique'], extra: true }));
  await act(() => root.render(h(List, { ids: ['unique', 'duplicate', 'duplicate'], extra: true })));
  assert.equal(calls.some(call => call.targets.some(node => node.dataset.flipId === 'duplicate')), false);
});

test('a large visible list animates exactly the first forty surviving items', async () => {
  const ids = Array.from({ length: 60 }, (_, index) => 'visible-' + index);
  await mount(h(List, { ids }));
  await act(() => root.render(h(List, { ids: [...ids.slice(0, 40).reverse(), ...ids.slice(40)] })));
  assert.equal(calls[0].targets.length, 40);
  assert.equal(calls[0].old.length, 40);
  assert.equal(calls[0].targets.some(node => Number(node.dataset.flipId.split('-')[1]) >= 40), false);
});

test('off-screen rows inside an overflow scroller are not treated as viewport-visible', async () => {
  layout = node => { const rect = defaultLayout(node);
    return node.className === 'list-root' ? { ...rect, height: 25, bottom: 45 } : rect; };
  function Clipped({ ids }) {
    const ref = React.useRef(null);
    useListFlip(ref, ids.join('|'));
    return h('div', { ref, className: 'list-root', style: { overflowY: 'auto' } },
      ids.map((id, index) => h('div', { key: id, 'data-flip-id': id, 'data-layout-index': index }, id)));
  }
  try {
    await mount(h(Clipped, { ids: ['a', 'b', 'c', 'clipped'] }));
    await act(() => root.render(h(Clipped, { ids: ['c', 'b', 'a', 'clipped'] })));
    assert.equal(calls[0].targets.length, 3);
    assert.equal(calls[0].targets.some(node => node.dataset.flipId === 'clipped'), false);
  } finally { layout = defaultLayout; }
});

for (const size of [200, 1000]) test(`${size} candidates: scroll measures exactly forty visible rows, never the whole list`, async () => {
  const ids = Array.from({ length: size }, (_, index) => 'row-' + index);
  await mount(h(List, { ids }));
  // Real GSAP 3.15 reads each row twice here: ElementState geometry plus
  // CSSPlugin's detached/offsetParent check (jsdom offsetParent is null).
  assert.equal(measured.filter(node => node.dataset.flipId).length, 80, 'initial capture measures only forty rows twice');
  assert.equal(new Set(measured).size, 40);
  measured.length = 0;
  const nearBottom = size - 60;
  scrollOffset = nearBottom * 10;
  await act(() => {
    for (const observer of intersectionObservers) observer.flush();
    for (let i = 0; i < 12; i++) window.dispatchEvent(new window.Event('scroll'));
  });
  await act(() => new Promise(resolve => window.requestAnimationFrame(resolve)));
  const rows = measured.filter(node => node.dataset.flipId);
  assert.equal(rows.length, 80, 'intersection and scroll notifications must coalesce into one bounded capture');
  assert.equal(new Set(rows).size, 40);
  assert.ok(rows.every(node => Number(node.dataset.flipId.slice(4)) >= nearBottom - 2), 'low visible rows cannot be missed');
  assert.equal(measured.length, 80, 'no manual ancestor rectangle walk or full-list layout probes');
  measured.length = 0;
  await act(() => window.dispatchEvent(new window.Event('scroll')));
  await act(() => new Promise(resolve => window.requestAnimationFrame(resolve)));
  assert.equal(measured.length, 80, 'steady scrolling also remains bounded');
});

test('native overflow intersections bound measurements to three low rows in a thousand-row list', async () => {
  scrollOffset = 8000;
  layout = node => { const rect = defaultLayout(node);
    return node.className === 'list-root' ? { ...rect, height: 25, bottom: 45 } : rect; };
  function Clipped() {
    const ref = React.useRef(null);
    useListFlip(ref, 'thousand');
    return h('div', { ref, className: 'list-root', style: { overflowY: 'auto' } },
      Array.from({ length: 1000 }, (_, i) => h('div', { key: i, 'data-flip-id': 'clipped-' + i, 'data-layout-index': i }, String(i))));
  }
  await mount(h(Clipped));
  assert.deepEqual(measured.map(node => node.dataset.flipId), [
    'clipped-800', 'clipped-800', 'clipped-801', 'clipped-801', 'clipped-802', 'clipped-802']);
  measured.length = 0;
  await act(() => document.querySelector('.list-root').dispatchEvent(new window.Event('scroll')));
  await act(() => new Promise(resolve => window.requestAnimationFrame(resolve)));
  assert.equal(measured.length, 6);
});

test('missing IntersectionObserver uses static rendering without full-list measurement', async () => {
  window.IntersectionObserver = undefined;
  try {
    const ids = Array.from({ length: 1000 }, (_, i) => 'static-' + i);
    await mount(h(List, { ids }));
    await act(() => root.render(h(List, { ids: [...ids].reverse() })));
    assert.equal(measured.length, 0);
    assert.equal(calls.length, 0);
    assert.equal(document.querySelectorAll('[data-flip-id]').length, 1000);
  } finally { window.IntersectionObserver = TestIntersectionObserver; }
});
