// v8 client regression: production client + real pure physics session. Only the
// Worker transport and clock are controlled. No browser, network or disk output.
// Run: node --test src/star-physics-client.test.mjs
import assert from 'node:assert/strict';
import test, { afterEach } from 'node:test';
import Module from 'node:module';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const sourceDir = fileURLToPath(new URL('.', import.meta.url));
class ManualClock {
  now = 1000;
  next = 1;
  timers = new Map();
  performance = { now: () => this.now };
  dateNow = () => 1800000000000 + this.now;
  timeout = (callback, delay = 0) => {
    const id = this.next++;
    this.timers.set(id, { callback, at: this.now + Math.max(0, Number(delay) || 0), interval: 0 });
    return id;
  };
  clear = id => this.timers.delete(id);
  interval = (callback, delay) => {
    const id = this.timeout(callback, delay);
    this.timers.get(id).interval = Math.max(1, Number(delay) || 1);
    return id;
  };
  advance(milliseconds) {
    const end = this.now + milliseconds;
    for (let turns = 0; ; turns++) {
      const due = [...this.timers].filter(([, timer]) => timer.at <= end + 1e-7)
        .sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      assert.ok(turns < 20000, 'timer work must be bounded');
      const [id, timer] = due;
      this.now = Math.max(this.now, timer.at);
      if (timer.interval) timer.at += timer.interval; else this.timers.delete(id);
      timer.callback();
    }
    this.now = end;
  }
}
const clock = new ManualClock();
const output = await build({
  stdin: { contents: 'export * from "./starPhysicsClient"; export { createStarPhysicsSession } from "./starGraphPhysics"; export { STAR_DEFAULT_PHYSICS } from "./starPhysicsSettings"; export { createStarfieldFixture } from "./starfieldFixture";', resolveDir: sourceDir, loader: 'ts' },
  bundle: true, platform: 'node', format: 'cjs', write: false, logLevel: 'silent',
  define: {
    setTimeout: '__clientClock.timeout', clearTimeout: '__clientClock.clear',
    'globalThis.setTimeout': '__clientClock.timeout', 'globalThis.clearTimeout': '__clientClock.clear',
    setInterval: '__clientClock.interval', clearInterval: '__clientClock.clear',
    performance: '__clientClock.performance', 'Date.now': '__clientClock.dateNow',
  },
});
const bundle = new Module(`${sourceDir}__client_test_memory.cjs`);
bundle.filename = `${sourceDir}__client_test_memory.cjs`;
bundle.paths = Module._nodeModulePaths(sourceDir);
// Inject the clock lexically; do not replace Node's runner timers or patch source.
bundle._compile(`module.exports = (__clientClock) => { ${output.outputFiles[0].text}\nreturn module.exports; };`, bundle.filename);
const star = bundle.exports(clock);
const fixtures = new Set();

class FakeWorker {
  messages = [];
  terminated = 0;
  throwOnPost = false;
  onmessage = null;
  onerror = null;
  onmessageerror = null;
  postMessage(message) {
    if (this.throwOnPost) throw new Error('synthetic postMessage failure');
    this.messages.push(structuredClone(message));
    this.session?.send(message);
  }
  emit(data) { this.onmessage?.({ data }); }
  terminate() { this.terminated++; this.session?.dispose(); }
}
function freeze(value) {
  if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value); }
  return value;
}
function client({ loopback = false, constructorThrows = false, postThrows = false, count = 7, budget } = {}) {
  const graph = freeze(star.createStarfieldFixture(count)), before = structuredClone(graph);
  const worker = new FakeWorker(), frames = [], backends = [];
  if (loopback) worker.session = star.createStarPhysicsSession(event => worker.emit(event), {
    now: () => clock.now, setTimeout: clock.timeout, clearTimeout: clock.clear,
  });
  worker.throwOnPost = postThrows;
  const runtime = star.createStarPhysicsClient({ graph, physics: { ...star.STAR_DEFAULT_PHYSICS },
    budget, focusId: graph.nodes[0].id,
    workerFactory() { if (constructorThrows) throw new Error('synthetic Worker unavailable'); return worker; },
    onFrame: frame => frames.push(structuredClone(frame)), onBackend: mode => backends.push(mode),
  });
  const fixture = { runtime, graph, before, worker, frames, backends,
    generation: () => worker.messages.at(-1)?.generation,
    frame(sequence = 1, extras = {}) {
      return { type: 'frame', generation: fixture.generation(), sequence,
        positions: Float32Array.from(graph.nodes.flatMap((_, i) => [i * 23, i * -11])),
        hot: false, ticks: 0, bounds: { left: -50, right: 200, top: -100, bottom: 50 }, ...extras };
    },
    dispose() { runtime.dispose(); fixtures.delete(fixture); },
  };
  fixtures.add(fixture);
  return fixture;
}

afterEach(() => {
  const active = [...fixtures];
  for (const fixture of active) fixture.dispose();
  // d3 may own a stopped-simulation housekeeping timer; let it become idle.
  clock.advance(100);
  for (const fixture of active) assert.deepEqual(fixture.graph, fixture.before, 'client must not mutate API graph input');
  assert.equal(clock.timers.size, 0, 'dispose must leave no session or startup timeout');
});

test('300 nodes / 600 edges and the synthetic budget reach Worker init unchanged', () => {
  const c = client({ count: 300, budget: 'synthetic-300' });
  const init = c.worker.messages[0];
  assert.equal(init.type, 'init'); assert.equal(init.mode, 'paused');
  assert.equal(init.budget, 'synthetic-300'); assert.equal(init.focusId, c.graph.nodes[0].id);
  assert.equal(init.graph.nodes.length, 300); assert.equal(init.graph.edges.length, 600);
  assert.deepEqual(init.graph, c.before);
  c.worker.emit(c.frame()); assert.equal(c.frames[0].positions.length, 600);
  assert.deepEqual(c.backends, ['worker']);
});

test('late generations, duplicate sequences and malformed positions cannot replace the accepted frame', () => {
  const c = client(), good = c.frame(4);
  c.worker.emit(good);
  for (const invalid of [
    c.frame(99, { generation: good.generation - 1 }), c.frame(99, { generation: good.generation + 1 }),
    c.frame(4), c.frame(3), c.frame(5, { positions: Array(14).fill(1) }),
    c.frame(5, { positions: new Float32Array(2) }),
    c.frame(5, { positions: new Float32Array(14).fill(NaN) }),
    c.frame(5, { positions: new Float32Array(14).fill(Infinity) }),
  ]) c.worker.emit(invalid);
  assert.equal(c.frames.length, 1);
  c.worker.emit(c.frame(5)); assert.equal(c.frames.length, 2, 'invalid frames must not consume a sequence number');
  clock.advance(2100); assert.deepEqual(c.backends, ['worker'], 'a valid first frame disarms the startup timeout');
});

test('malformed sequence numbers do not poison ordering', () => {
  const c = client();
  for (const sequence of [NaN, Infinity, 1.5, undefined]) c.worker.emit(c.frame(sequence, { sequence }));
  assert.equal(c.frames.length, 0, 'sequence must be a finite safe integer before acceptance');
  c.worker.emit(c.frame(1)); assert.equal(c.frames.length, 1);
});

test('topology replacement isolates a disposed client even when its old callback is already queued', () => {
  const old = client(), queued = old.worker.onmessage, stale = old.frame(500);
  old.dispose();
  const current = client({ count: 50 });
  assert.ok(current.generation() > stale.generation);
  queued({ data: stale }); current.worker.emit({ ...stale, generation: stale.generation });
  assert.equal(old.frames.length, 0); assert.equal(current.frames.length, 0);
  current.worker.emit(current.frame(1)); assert.equal(current.frames.length, 1);
});

test('configure advances generation and rejects queued positions from the previous settings', () => {
  const c = client(), old = c.frame(10);
  c.worker.emit(old);
  c.runtime.send({ type: 'configure', physics: { ...star.STAR_DEFAULT_PHYSICS, distance: 160 } });
  const configure = c.worker.messages.at(-1);
  assert.ok(configure.generation > old.generation, 'protocol requires configure.generation > the active generation');
  c.worker.emit({ ...old, sequence: 999 }); assert.equal(c.frames.length, 1);
  c.worker.emit(c.frame(1)); assert.equal(c.frames.length, 2, 'new generation resets frame sequence');
  c.runtime.send({ type: 'pin', id: c.graph.nodes[0].id, x: 70, y: 80 });
  assert.equal(c.worker.messages.at(-1).generation, configure.generation);
});

test('configure is accepted by the real session, including its generation and sequence reset', () => {
  const c = client({ loopback: true });
  const initial = c.frames.at(-1);
  c.runtime.send({ type: 'configure', physics: { ...star.STAR_DEFAULT_PHYSICS, repel: 300 } });
  assert.equal(c.frames.length, 2, 'real session must publish the configured generation instead of silently ignoring it');
  assert.ok(c.frames.at(-1).generation > initial.generation); assert.equal(c.frames.at(-1).sequence, 1);
});

for (const trigger of ['error-event', 'message-error', 'protocol-error', 'post-error']) {
  test(`${trigger} switches to real main-thread physics once and terminates Worker handlers`, () => {
    const c = client();
    const latest = c.frame(10); c.worker.emit(latest);
    const oldError = c.worker.onerror, oldMessageError = c.worker.onmessageerror;
    let prevented = false;
    if (trigger === 'error-event') oldError({ preventDefault() { prevented = true; } });
    if (trigger === 'message-error') oldMessageError({});
    if (trigger === 'protocol-error') c.worker.emit({ type: 'error', generation: c.generation(), message: 'synthetic worker failure' });
    if (trigger === 'post-error') { c.worker.throwOnPost = true; c.runtime.send({ type: 'mode', mode: 'running' }); }
    if (trigger === 'error-event') assert.equal(prevented, true);
    assert.deepEqual(c.backends, ['worker', 'main-thread']);
    assert.equal(c.worker.terminated, 1);
    assert.equal(c.worker.onmessage, null); assert.equal(c.worker.onerror, null); assert.equal(c.worker.onmessageerror, null);
    oldError({ preventDefault() {} }); oldMessageError({});
    assert.deepEqual(c.backends, ['worker', 'main-thread']); assert.equal(c.worker.terminated, 1);
    c.runtime.send({ type: 'mode', mode: 'running' });
    const before = c.frames.length; clock.advance(100);
    assert.ok(c.frames.length > before, 'fallback must produce actual d3 positions');
    assert.ok(c.frames.at(-1).ticks > 0);
  });
}

test('startup timeout fires at 2000ms after invalid or wrong-generation messages, then runs real fallback', () => {
  const c = client();
  c.runtime.send({ type: 'mode', mode: 'running' });
  c.worker.emit(c.frame(1, { generation: c.generation() - 1 }));
  c.worker.emit(c.frame(1, { positions: new Float32Array(1) }));
  clock.advance(1999); assert.deepEqual(c.backends, ['worker']);
  clock.advance(1); assert.deepEqual(c.backends, ['worker', 'main-thread']);
  assert.equal(c.worker.terminated, 1); assert.ok(c.frames.length > 0);
  clock.advance(50); assert.ok(c.frames.at(-1).ticks > 0);
});

test('an error with an obsolete generation does not trigger fallback', () => {
  const c = client(); c.worker.emit(c.frame(1));
  c.worker.emit({ type: 'error', generation: c.generation() - 1, message: 'late error' });
  assert.deepEqual(c.backends, ['worker']); assert.equal(c.worker.terminated, 0);
});

for (const failure of ['constructorThrows', 'postThrows']) test(`${failure} starts usable fallback without Worker availability`, () => {
  const c = client({ [failure]: true });
  assert.deepEqual(c.backends, ['main-thread']);
  assert.ok(c.frames.length > 0); assert.ok(c.frames[0].positions.every(Number.isFinite));
  c.runtime.send({ type: 'mode', mode: 'running' }); clock.advance(50);
  assert.ok(c.frames.at(-1).ticks > 0);
});

test('fallback retains the last valid coordinates and active pin before continuing', () => {
  const c = client(), latest = c.frame(10);
  c.worker.emit(latest);
  const id = c.graph.nodes[0].id;
  c.runtime.send({ type: 'pin', id, x: 123, y: -54 });
  c.runtime.send({ type: 'mode', mode: 'running' });
  c.worker.onmessageerror({});
  const seeded = c.frames.at(-1);
  assert.equal(seeded.positions[0], 123); assert.equal(seeded.positions[1], -54);
  assert.deepEqual(seeded.positions.slice(2), latest.positions.slice(2), 'seeding must not expose a layout jump');
  clock.advance(100);
  assert.equal(c.frames.at(-1).positions[0], 123); assert.equal(c.frames.at(-1).positions[1], -54);
  c.runtime.send({ type: 'release', id, reheat: true }); clock.advance(100);
  assert.notEqual(c.frames.at(-1).positions[0], 123, 'released fallback pin must move again');
});

test('queued old Worker frames cannot overwrite main-thread positions after fallback', () => {
  const c = client(), queued = c.worker.onmessage;
  c.worker.emit(c.frame(10)); c.runtime.send({ type: 'mode', mode: 'running' });
  c.worker.onmessageerror({}); clock.advance(40);
  const before = c.frames.length, positions = c.frames.at(-1).positions;
  queued({ data: c.frame(10000, { positions: new Float32Array(14).fill(777) }) });
  assert.equal(c.frames.length, before, 'terminated Worker callback must be invalidated even within the same client generation');
  assert.deepEqual(c.frames.at(-1).positions, positions);
  clock.advance(40); assert.ok(c.frames.length > before, 'stale Worker sequence must not starve future fallback frames');
});

for (const mode of ['paused', 'quiet']) test(`fallback in ${mode} retains coordinates without unsolicited force ticks`, () => {
  const c = client(); c.worker.emit(c.frame(1));
  c.runtime.send({ type: 'mode', mode }); c.worker.onmessageerror({});
  const last = c.frames.at(-1); clock.advance(500);
  assert.deepEqual(c.frames.at(-1), last);
  c.runtime.send({ type: 'pin', id: c.graph.nodes[1].id, x: 200, y: 100 });
  c.runtime.send({ type: 'release', id: c.graph.nodes[1].id, reheat: false });
  const released = c.frames.at(-1); clock.advance(500);
  assert.deepEqual(c.frames.at(-1), released); assert.equal(released.ticks, 0);
});

test('dispose cancels startup timeout and ignores saved message/error callbacks and future commands', () => {
  const c = client(), message = c.worker.onmessage, error = c.worker.onerror, malformed = c.worker.onmessageerror;
  const timers = [...clock.timers.values()], frame = c.frame(1), posted = c.worker.messages.length;
  c.runtime.dispose(); c.runtime.dispose();
  for (const timer of timers) timer.callback();
  message({ data: frame }); error({ preventDefault() {} }); malformed({});
  c.runtime.send({ type: 'replay' }); c.runtime.send({ type: 'mode', mode: 'running' });
  clock.advance(3000);
  assert.equal(c.worker.terminated, 1); assert.equal(c.worker.messages.length, posted);
  assert.equal(c.frames.length, 0); assert.deepEqual(c.backends, ['worker']);
});

test('dispose cancels running fallback and rejects even a previously queued physics timer', () => {
  const c = client({ constructorThrows: true });
  c.runtime.send({ type: 'mode', mode: 'running' }); clock.advance(34);
  const pending = [...clock.timers.values()], count = c.frames.length;
  c.runtime.dispose(); c.runtime.dispose();
  for (const timer of pending) timer.callback();
  c.runtime.send({ type: 'pin', id: c.graph.nodes[0].id, x: 100, y: 100 });
  clock.advance(3000); assert.equal(c.frames.length, count);
  assert.deepEqual(c.backends, ['main-thread']);
});
