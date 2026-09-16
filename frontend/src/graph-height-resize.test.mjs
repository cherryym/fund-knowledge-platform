import test, { after, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { transform } from 'esbuild';
import { JSDOM } from 'jsdom';
const source = await readFile(new URL('./graphStageResize.ts', import.meta.url), 'utf8');
const code = await transform(source, { loader: 'ts', format: 'esm' });
const { attachGraphHeightResize, graphHeightLimits } = await import('data:text/javascript;base64,' + Buffer.from(code.code).toString('base64'));
const dom = new JSDOM('<!doctype html><body></body>', { url: 'http://localhost', pretendToBeVisual: true });
globalThis.window = dom.window; globalThis.document = dom.window.document;
Object.defineProperty(window, 'innerWidth', { value: 1440, writable: true });
Object.defineProperty(window, 'innerHeight', { value: 1080, writable: true });
const frames = new Map(); let sequence = 0; let cleanups = [];
window.requestAnimationFrame = fn => { const id = ++sequence; frames.set(id, fn); return id; };
window.cancelAnimationFrame = id => frames.delete(id);
const flush = () => { const calls = [...frames.values()]; frames.clear(); calls.forEach(fn => fn(0)); };
const key = value => `fkb:ui:graph-height:v1:${encodeURIComponent(value)}`;
function setup(preference = 'user-a:space-a:global', saved) {
  if (saved !== undefined) window.localStorage.setItem(key(preference), saved);
  const stage = document.createElement('div'), handle = document.createElement('div'); handle.tabIndex = 0;
  document.body.append(stage, handle); const captures = new Set();
  handle.setPointerCapture = id => captures.add(id); handle.hasPointerCapture = id => captures.has(id); handle.releasePointerCapture = id => captures.delete(id);
  const dispose = attachGraphHeightResize(handle, stage, preference); cleanups.push(dispose);
  const emit = (type, fields = {}) => { const event = new window.Event(type, { bubbles: true, cancelable: true });
    Object.assign(event, { pointerId: 1, isPrimary: true, button: 0, clientY: 700, ...fields }); handle.dispatchEvent(event); return event; };
  return { stage, handle, captures, emit, dispose, height: () => Number(handle.getAttribute('aria-valuenow')), stored: () => window.localStorage.getItem(key(preference)) };
}
afterEach(() => { cleanups.forEach(fn => fn()); cleanups = []; assert.equal(frames.size, 0); document.body.replaceChildren(); window.localStorage.clear(); window.innerWidth = 1440; window.innerHeight = 1080; });
after(() => dom.window.close());

test('desktop defaults grow with viewport height and mobile stays bounded', () => {
  assert.equal(graphHeightLimits(1440, 1080).defaultHeight, 842);
  assert.equal(graphHeightLimits(1440, 768).defaultHeight, 680);
  assert.equal(graphHeightLimits(1440, 1800).defaultHeight, 1100);
  assert.equal(graphHeightLimits(390, 844).defaultHeight, 591);
  assert.equal(graphHeightLimits(NaN, NaN).defaultHeight, 702);
});
test('100 vertical moves coalesce into one frame, release flushes the exact height and saves only a number', () => {
  const e = setup(); e.emit('pointerdown');
  for (let i = 1; i <= 100; i++) e.emit('pointermove', { clientY: 700 + i * 2 });
  assert.equal(frames.size, 1); assert.equal(e.height(), 842); assert.equal(e.stored(), null);
  flush(); assert.equal(e.height(), 1042); assert.equal(e.stage.dataset.heightResizing, 'true');
  e.emit('pointerup', { clientY: 950 }); assert.equal(e.height(), 1092); assert.equal(e.stored(), '1092');
  assert.equal(e.captures.size, 0); assert.equal(e.stage.dataset.heightResizing, undefined);
});
test('up/down drags respect minimum and maximum without waiting for a final frame', () => {
  const e = setup(); e.emit('pointerdown'); e.emit('pointerup', { clientY: -5000 }); assert.equal(e.height(), 400);
  e.emit('pointerdown'); e.emit('pointermove', { clientY: 10000 }); e.emit('pointerup', { clientY: 10000 });
  assert.equal(e.height(), 2160); assert.equal(frames.size, 0);
});
for (const name of ['pointercancel', 'lostpointercapture', 'blur', 'Escape']) test(`${name} restores the pre-drag preference and releases capture`, () => {
  const e = setup(undefined, '900'); e.emit('pointerdown'); e.emit('pointermove', { clientY: 850 }); flush(); assert.equal(e.height(), 1050);
  if (name === 'blur') window.dispatchEvent(new window.Event('blur'));
  else if (name === 'Escape') e.emit('keydown', { key: 'Escape' }); else e.emit(name);
  assert.equal(e.height(), 900); assert.equal(e.stored(), '900'); assert.equal(e.captures.size, 0);
});
test('keyboard is accessible and double click clears the preference for responsive defaults', () => {
  const e = setup(); e.emit('keydown', { key: 'ArrowDown' }); assert.equal(e.height(), 866);
  e.emit('keydown', { key: 'ArrowUp', shiftKey: true }); assert.equal(e.height(), 786);
  e.emit('keydown', { key: 'End' }); assert.equal(e.height(), 2160); e.emit('keydown', { key: 'Home' }); assert.equal(e.height(), 400);
  e.emit('dblclick'); assert.equal(e.height(), 842); assert.equal(e.stored(), null);
  window.innerHeight = 1440; window.dispatchEvent(new window.Event('resize')); assert.equal(e.height(), 1100);
  assert.equal(e.handle.getAttribute('aria-valuetext'), '画布高度 1100 像素');
});
test('preferences are separate for users/spaces/local views and restore across remounts', () => {
  const e = setup(); e.emit('keydown', { key: 'ArrowDown' }); e.dispose();
  assert.equal(setup().height(), 866); assert.equal(setup('user-b:space-a:global').height(), 842);
  assert.equal(setup('user-a:space-b:global').height(), 842); assert.equal(setup('user-a:space-a:local').height(), 842);
});
test('narrow windows clamp but do not overwrite a larger preferred height', () => {
  const e = setup(undefined, '2000'); window.innerHeight = 650; window.dispatchEvent(new window.Event('resize'));
  assert.equal(e.height(), 1300); assert.equal(e.stored(), '2000'); window.innerHeight = 1200;
  window.dispatchEvent(new window.Event('resize')); assert.equal(e.height(), 2000);
});
test('secondary buttons, nonprimary touches and other pointer IDs cannot hijack the resize', () => {
  const e = setup(); e.emit('pointerdown', { button: 2 }); e.emit('pointerdown', { isPrimary: false }); assert.equal(e.captures.size, 0);
  e.emit('pointerdown'); e.emit('pointermove', { pointerId: 2, clientY: 900 }); e.emit('pointerup', { pointerId: 2 });
  assert.equal(e.captures.size, 1); assert.equal(frames.size, 0); assert.equal(e.height(), 842);
});
test('unmount cancels pending work and stale callbacks cannot change the stage', () => {
  const e = setup(); e.emit('pointerdown'); e.emit('pointermove', { clientY: 900 }); const late = [...frames.values()][0];
  e.dispose(); late(); window.dispatchEvent(new window.Event('resize')); e.emit('keydown', { key: 'ArrowDown' });
  assert.equal(e.stage.style.getPropertyValue('--star-stage-height'), ''); assert.equal(e.captures.size, 0);
});
test('invalid or unavailable storage never prevents resizing; anonymous graphs do not persist', () => {
  assert.equal(setup(undefined, 'NaN').height(), 842);
  const anonymous = setup(''); anonymous.emit('keydown', { key: 'ArrowDown' }); assert.equal(window.localStorage.getItem(key('')), null);
  const get = window.Storage.prototype.getItem, set = window.Storage.prototype.setItem;
  window.Storage.prototype.getItem = () => { throw Error('storage disabled'); }; window.Storage.prototype.setItem = () => { throw Error('storage disabled'); };
  try { const e = setup('private-mode'); e.emit('keydown', { key: 'ArrowDown' }); assert.equal(e.height(), 866); }
  finally { window.Storage.prototype.getItem = get; window.Storage.prototype.setItem = set; }
});
test('height is not a physics dependency and canvas/SVG both fill the resizable stage', async () => {
  const graph = await readFile(new URL('./KnowledgeGraph.tsx', import.meta.url), 'utf8');
  const css = await readFile(new URL('./star-graph.css', import.meta.url), 'utf8');
  assert.match(graph, /<GraphHeightResizeHandle stage=\{stage\}/);
  assert.match(graph, /dependencies: \[graph, focusId, renderBudget, useCanvas, diagnostics\]/);
  assert.match(css, /\.star-surface \{[^}]*height: 100%/); assert.match(css, /\.star-canvas-stack \{[^}]*height: 100%/);
  assert.doesNotMatch(source, /fetch\(|onOpenNode|replay\(|mountStarCanvas|mountStarGraph/);
});
