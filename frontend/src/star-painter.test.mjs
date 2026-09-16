// Execute the real painter with strict WebGL/Canvas2D API stand-ins. Assertions
// concern actual drawArrays calls, uploaded vertices, state and resource lifetime.
// No shader rasterization, browser, GPU presentation, FPS or latency is measured.
import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const output = await build({
  entryPoints: [fileURLToPath(new URL('./starPainter.ts', import.meta.url))],
  bundle: true, write: false, platform: 'node', format: 'esm', logLevel: 'silent',
});
const painterModule = await import(`data:text/javascript;base64,${Buffer.from(output.outputFiles[0].text).toString('base64')}`);
const { createStarPainter } = painterModule;

class GL {
  VERTEX_SHADER = 0x8b31; FRAGMENT_SHADER = 0x8b30; COMPILE_STATUS = 0x8b81; LINK_STATUS = 0x8b82;
  ARRAY_BUFFER = 0x8892; DYNAMIC_DRAW = 0x88e8; FLOAT = 0x1406; POINTS = 0; TRIANGLES = 4;
  ALIASED_POINT_SIZE_RANGE = 0x846d; MAX_VIEWPORT_DIMS = 0x0d3a; MAX_RENDERBUFFER_SIZE = 0x84e8;
  DEPTH_TEST = 0x0b71; CULL_FACE = 0x0b44; SCISSOR_TEST = 0x0c11; BLEND = 0x0be2;
  FUNC_ADD = 0x8006; ONE = 1; ONE_MINUS_SRC_ALPHA = 0x0303; COLOR_BUFFER_BIT = 0x4000; NO_ERROR = 0;
  shaders = new Map(); programs = new Map(); buffers = new Map();
  created = { shader: [], program: [], buffer: [] };
  deleted = { shader: [], program: [], buffer: [] };
  attempts = { shader: 0, program: 0, buffer: 0 };
  attributes = new Map(); enabledAttributes = new Set(); enabled = new Set();
  uploads = []; allocations = []; draws = []; clears = []; viewports = []; sources = [];
  queries = { errors: 0, parameters: 0 };
  error = 0; lost = false; bound = null; current = null; next = 0;
  constructor(options = {}) { this.options = options; this.error = options.initializationError ?? 0; }
  make(kind, state) {
    const attempt = ++this.attempts[kind];
    if (this.options[`fail${kind[0].toUpperCase()+kind.slice(1)}At`] === attempt) return null;
    const handle = { kind, id: ++this.next };
    this[`${kind}s`].set(handle, state); this.created[kind].push(handle);
    return handle;
  }
  remove(kind, handle) {
    assert.ok(this[`${kind}s`].has(handle), `delete live ${kind} exactly once`);
    this[`${kind}s`].delete(handle); this.deleted[kind].push(handle);
  }
  createShader(type) { return this.make('shader', { type }); }
  shaderSource(shader, source) { this.shaders.get(shader).source = source; this.sources.push(source); }
  compileShader(shader) { this.shaders.get(shader).compiled = this.options.failCompileAt !== this.attempts.shader; }
  getShaderParameter(shader, key) { assert.equal(key, this.COMPILE_STATUS); return this.shaders.get(shader).compiled; }
  deleteShader(shader) { this.remove('shader', shader); }
  createProgram() { return this.make('program', { shaders: [], names: new Map() }); }
  attachShader(program, shader) { this.programs.get(program).shaders.push(shader); }
  detachShader(program, shader) {
    const state = this.programs.get(program); state.shaders = state.shaders.filter(item => item !== shader);
  }
  bindAttribLocation(program, location, name) { this.programs.get(program).names.set(location, name); }
  linkProgram(program) {
    const state = this.programs.get(program);
    state.linked = this.options.failLinkAt !== this.attempts.program && state.shaders.every(shader => this.shaders.get(shader).compiled);
  }
  getProgramParameter(program, key) { assert.equal(key, this.LINK_STATUS); return this.programs.get(program).linked; }
  getUniformLocation(program, name) { return this.options.missingUniform ? null : { program, name }; }
  deleteProgram(program) { this.remove('program', program); }
  useProgram(program) {
    assert.ok(program === null || this.programs.get(program)?.linked);
    this.current = program;
  }
  uniform2f(location, x, y) {
    assert.equal(location.program, this.current); assert.equal(location.name, 'u_view');
    assert.ok(x > 0 && y > 0); this.programs.get(this.current).view = [x, y];
  }
  createBuffer() { return this.make('buffer', { data: null }); }
  bindBuffer(target, buffer) {
    assert.equal(target, this.ARRAY_BUFFER); assert.ok(buffer === null || this.buffers.has(buffer)); this.bound = buffer;
  }
  bufferData(target, bytes, usage) {
    assert.equal(target, this.ARRAY_BUFFER); assert.equal(usage, this.DYNAMIC_DRAW);
    assert.equal(typeof bytes, 'number'); assert.equal(bytes % 4, 0); assert.ok(bytes > 0);
    this.buffers.get(this.bound).data = new Float32Array(bytes/4);
    this.allocations.push({ buffer: this.bound, bytes });
  }
  bufferSubData(target, offset, source) {
    assert.equal(target, this.ARRAY_BUFFER); assert.equal(offset, 0); assert.ok(source instanceof Float32Array);
    const state = this.buffers.get(this.bound);
    assert.ok(state?.data && source.byteLength <= state.data.byteLength, 'upload must fit the allocated GPU buffer');
    state.data.set(source);
    this.uploads.push({ buffer: this.bound, source, arrayBuffer: source.buffer });
  }
  deleteBuffer(buffer) {
    if (this.bound === buffer) this.bound = null;
    this.remove('buffer', buffer);
  }
  vertexAttribPointer(index, size, type, normalized, stride, offset) {
    assert.ok(this.bound); assert.equal(type, this.FLOAT); assert.equal(normalized, false);
    assert.equal(stride % 4, 0); assert.equal(offset % 4, 0);
    this.attributes.set(index, { buffer: this.bound, size, stride, offset });
  }
  enableVertexAttribArray(index) { this.enabledAttributes.add(index); }
  disableVertexAttribArray(index) { this.enabledAttributes.delete(index); }
  enable(capability) { this.enabled.add(capability); }
  disable(capability) { this.enabled.delete(capability); }
  blendEquation(mode) { this.blendMode = mode; }
  blendFunc(source, destination) { this.blend = [source, destination]; }
  clearColor(...values) { this.clearRGBA = values; }
  viewport(...values) { this.viewports.push(values); }
  clear(mask) { assert.equal(mask, this.COLOR_BUFFER_BIT); this.clears.push(mask); }
  getParameter(key) {
    this.queries.parameters++;
    if (key === this.ALIASED_POINT_SIZE_RANGE) return new Float32Array([1, this.options.maxPoint ?? 64]);
    if (key === this.MAX_VIEWPORT_DIMS) return new Int32Array([this.options.maxBitmap ?? 8192, this.options.maxBitmap ?? 8192]);
    if (key === this.MAX_RENDERBUFFER_SIZE) return this.options.maxBitmap ?? 8192;
    assert.fail(`unexpected WebGL parameter ${key}`);
  }
  getError() { this.queries.errors++; const error = this.error; this.error = 0; return error; }
  isContextLost() { return this.lost; }
  drawArrays(mode, first, count) {
    // Native WebGL ignores subsequent draw commands after context loss; it does
    // not throw. The painter must detect loss even after the LAST batch returns.
    if (this.lost) return;
    if (this.throwOnDraw) throw new Error('injected command failure');
    assert.ok(this.current && this.bound);
    assert.ok(mode === this.POINTS || mode === this.TRIANGLES); assert.equal(first, 0); assert.ok(count > 0);
    if (mode === this.TRIANGLES) assert.equal(count % 6, 0, 'each quad emits two complete triangles');
    const program = this.programs.get(this.current); assert.ok(program.view);
    const attrs = {};
    for (const [location, name] of program.names) {
      assert.ok(this.enabledAttributes.has(location), `${name} must be enabled`);
      const attribute = this.attributes.get(location), buffer = this.buffers.get(attribute.buffer);
      assert.ok(buffer.data.byteLength >= attribute.offset + (count-1)*attribute.stride + attribute.size*4,
        `${name} fetch must stay within GPU allocation`);
      attrs[name] = Array.from({ length: count }, (_, i) => Array.from(buffer.data.subarray(
        (attribute.offset+i*attribute.stride)/4, (attribute.offset+i*attribute.stride)/4+attribute.size)));
      for (const values of attrs[name]) assert.ok(values.every(Number.isFinite), `${name} must never contain NaN/Infinity`);
    }
    this.draws.push({ mode, count, buffer: this.bound, program: this.current, view: program.view, attrs });
    if (this.loseDuringDraw) this.lost = true;
  }
  assertReleased() {
    assert.equal(this.buffers.size, 0); assert.equal(this.programs.size, 0); assert.equal(this.shaders.size, 0);
    assert.equal(this.enabledAttributes.size, 0); assert.equal(this.current, null); assert.equal(this.bound, null);
    for (const kind of ['shader', 'program', 'buffer']) assert.deepEqual(this.deleted[kind].slice().sort((a,b) => a.id-b.id), this.created[kind]);
  }
}

class Context2D {
  calls = []; measures = 0; transform = [1,0,0,1,0,0];
  constructor(canvas) { this.canvas = canvas; this.reset(); }
  reset() { this.globalAlpha = 1; this.font = '10px sans-serif'; this.textAlign = 'start'; this.fillStyle = '#000'; this.path = []; }
  setTransform(...values) { this.transform = values; this.calls.push({ type: 'transform', values }); }
  clearRect(...values) { this.calls.push({ type: 'clear', values, transform: this.transform.slice() }); }
  beginPath() { this.path = []; }
  moveTo(...values) { this.path.push({ type: 'move', values }); }
  lineTo(...values) { this.path.push({ type: 'line', values }); }
  arc(...values) { assert.ok(values.every(Number.isFinite)); assert.ok(values[2] > 0); this.path.push({ type: 'arc', values }); }
  stroke() { this.calls.push({ type: 'stroke', path: this.path.slice(), width: this.lineWidth, color: this.strokeStyle, alpha: this.globalAlpha }); }
  fill() { this.calls.push({ type: 'fill', path: this.path.slice(), color: this.fillStyle, alpha: this.globalAlpha }); }
  createRadialGradient(...values) {
    const gradient = { values, stops: [], addColorStop(offset, color) { this.stops.push([offset, color]); } };
    this.calls.push({ type: 'gradient', gradient }); return gradient;
  }
  measureText(text) {
    assert.ok(this.font.startsWith('12px ')); this.measures++;
    return { width: Array.from(text).reduce((sum, char) => sum + (char.codePointAt(0) > 127 ? 12 : 6), 0) };
  }
  fillText(text, x, y) {
    if (this.throwText) throw new Error('2D drawing failed');
    this.calls.push({ type: 'text', text, x, y, alpha: this.globalAlpha, font: this.font,
      color: this.fillStyle, align: this.textAlign, transform: this.transform.slice() });
  }
}
class Canvas extends EventTarget {
  requests = []; listeners = new Map(); sizes = []; contextKind = null; _width = 300; _height = 150;
  constructor(gl = null, options = {}) { super(); this.gl = gl; this.options = options; this.context = new Context2D(this); }
  get width() { return this._width; }
  set width(value) { this._width = value; this.sizes.push(['width', value]); this.context.reset(); }
  get height() { return this._height; }
  set height(value) { this._height = value; this.sizes.push(['height', value]); this.context.reset(); }
  getContext(kind, options) {
    this.requests.push({ kind, options });
    if (this.options.throwWebGL && kind === 'webgl') throw new Error('WebGL disabled');
    if (this.contextKind && this.contextKind !== kind) return null; // Browser's context-mode lock.
    if (kind === 'webgl' && this.gl) { this.contextKind = kind; return this.gl; }
    if (kind === '2d' && !this.options.no2d) { this.contextKind = kind; return this.context; }
    return null;
  }
  addEventListener(type, listener) { super.addEventListener(type, listener); this.listeners.set(type, listener); }
  removeEventListener(type, listener) {
    assert.equal(this.listeners.get(type), listener); super.removeEventListener(type, listener); this.listeners.delete(type);
  }
  lose() {
    this.gl.lost = true;
    const event = new Event('webglcontextlost', { cancelable: true }); this.dispatchEvent(event); return event;
  }
  restore() { this.gl.lost = false; this.dispatchEvent(new Event('webglcontextrestored')); }
}
function setup(options = {}) {
  const gl = options.noWebGL ? null : new GL(options.gl);
  const canvas = new Canvas(gl, options.canvas), labels = new Canvas(null, options.labels);
  const state = { invalidations: 0 };
  const painter = createStarPainter(canvas, labels, () => { state.invalidations++; });
  return { gl, canvas, labels, painter, state };
}
const node = (id, patch = {}) => ({ id, x: 100, y: 100, radius: 5, color: '#123456', opacity: 1, halo: 0,
  label: `节点 ${id}`, labelOpacity: 1, ...patch });
const edge = (source, target, patch = {}) => ({ source, target, color: '#abcdef', opacity: 0.4, width: 2, ...patch });
const frame = (nodes = [node('a'), node('b', { x: 200 })], edges = [edge(0, 1)], camera = { x: 0, y: 0, k: 1 }) => ({ nodes, edges, camera });
const close = (a, b, message) => assert.ok(Math.abs(a-b) < 1e-5, `${message ?? ''}: ${a} ~= ${b}`);
const typedDraw = (gl, name) => gl.draws.filter(draw => draw.attrs[name]);
const texts = labels => labels.context.calls.filter(call => call.type === 'text');

test('exports the contract; a frame emits real edge triangles, point attributes and separate labels', () => {
  assert.deepEqual(Object.keys(painterModule), ['createStarPainter']);
  const { gl, canvas, labels, painter } = setup();
  assert.deepEqual(Object.keys(painter).sort(), ['dispose', 'draw', 'mode', 'resize']);
  assert.equal(Object.getOwnPropertyDescriptor(painter, 'mode').set, undefined);
  assert.equal(painter.mode, 'webgl'); assert.equal(painter.draw(frame()), false, 'must resize before submission');
  painter.resize(960, 620, 3);
  assert.deepEqual([canvas.width, canvas.height, labels.width, labels.height], [1920, 1240, 1920, 1240]);
  assert.equal(painter.draw(frame()), true);
  assert.equal(gl.draws.length, 2);
  const [line, dots] = gl.draws;
  assert.equal(line.mode, gl.TRIANGLES); assert.equal(line.count, 6);
  assert.deepEqual(line.attrs.a_position, [[100,101],[100,99],[200,99],[100,101],[200,99],[200,101]]);
  assert.equal(dots.mode, gl.POINTS); assert.equal(dots.count, 2);
  assert.deepEqual(dots.attrs.a_position, [[100,100],[200,100]]);
  assert.deepEqual(dots.attrs.a_size, [[20],[20]]);
  dots.attrs.a_color[0].forEach((value, i) => close(value, [0x12/255,0x34/255,0x56/255,1][i]));
  assert.deepEqual(gl.blend, [gl.ONE, gl.ONE_MINUS_SRC_ALPHA]); assert.equal(gl.blendMode, gl.FUNC_ADD);
  assert.deepEqual(gl.clearRGBA, [0,0,0,0]); assert.ok(gl.enabled.has(gl.BLEND));
  assert.equal(gl.enabled.has(gl.DEPTH_TEST), false);
  assert.equal(canvas.requests.some(request => request.kind === '2d'), false);
  assert.equal(labels.requests[0].kind, '2d');
  assert.deepEqual(texts(labels).map(item => [item.text,item.x,item.y,item.transform]),
    [['节点 a',112,100,[2,0,0,2,0,0]],['节点 b',212,100,[2,0,0,2,0,0]]]);
  assert.equal(gl.created.shader.length, 4); assert.equal(gl.shaders.size, 0, 'linked programs release shader objects eagerly');
  painter.dispose(); gl.assertReleased(); assert.equal(canvas.listeners.size, 0);
});

test('letterbox + camera transform agrees with world coordinates, including vertical and diagonal edge widths', () => {
  const { gl, labels, painter } = setup();
  painter.resize(1200, 620, 1.5);
  const current = frame([node('a', { x: 10, y: 20, radius: 4 }), node('b', { x: 10, y: 50 }), node('c', { x: 40, y: 60 })],
    [edge(0, 1, { width: 3 }), edge(0, 2, { width: 5 })], { x: 20, y: -10, k: 2 });
  assert.equal(painter.draw(current), true);
  const [lines, dots] = gl.draws;
  assert.deepEqual(dots.view, [1200,620]); assert.deepEqual(dots.attrs.a_position, [[160,30],[160,90],[220,110]]);
  close(dots.attrs.a_size[0][0], 24);
  for (const [index, expected] of [[0,6],[6,10]]) {
    const [a,b] = lines.attrs.a_position.slice(index,index+2); close(Math.hypot(a[0]-b[0],a[1]-b[1]), expected);
  }
  assert.equal(texts(labels)[0].x, 175); assert.ok(texts(labels).every(item => item.font.startsWith('12px ')));
  painter.resize(960, 1000, 1);
  assert.equal(painter.draw(frame([node('a', { x: 0, y: 0 })], [])), true);
  assert.deepEqual(gl.draws.at(-1).attrs.a_position, [[0,190]], 'vertical letterbox centers the 620 world canvas');
  painter.dispose(); gl.assertReleased();
});

test('camera and color updates change submitted buffers immediately; unchanged capacities reuse GPU/TypedArray storage', () => {
  const { gl, labels, painter } = setup(); painter.resize(960,620,2);
  const current = frame(); painter.draw(current);
  const firstUploads = gl.uploads.slice(), allocated = gl.allocations.length, created = structuredClone(gl.attempts);
  const measured = labels.context.measures;
  for (let index = 1; index <= 12; index++) {
    current.camera.x = index; current.nodes[0].color = index % 2 ? '#f008' : '#00ff0080';
    assert.equal(painter.draw(current), true);
    const uploaded = gl.uploads.slice(-2);
    uploaded.forEach((upload, i) => {
      assert.equal(upload.buffer, firstUploads[i].buffer); assert.equal(upload.source, firstUploads[i].source);
      assert.equal(upload.arrayBuffer, firstUploads[i].arrayBuffer);
    });
    assert.equal(gl.draws.at(-1).attrs.a_position[0][0], 100+index);
  }
  assert.equal(gl.allocations.length, allocated); assert.deepEqual(gl.attempts, created);
  assert.equal(labels.context.measures, measured, 'stable labels reuse measured text across camera frames');
  const rgba = gl.draws.at(-1).attrs.a_color[0]; assert.deepEqual(rgba.slice(0,3), [0,1,0]); close(rgba[3],128/255);
  painter.dispose(); gl.assertReleased();
});

test('300/600 and 600/1200 fixtures retain every visible node and edge in bounded batches; growth/shrink never draws stale tails', () => {
  const { gl, painter } = setup(); painter.resize(960,620,1);
  for (const count of [300,600,3]) {
    const nodes = Array.from({ length: count }, (_, i) => node(`${i}`, { x: 20+(i%30)*30, y: 20+Math.floor(i/30)*25, labelOpacity: 0 }));
    const edges = Array.from({ length: count*2 }, (_, i) => edge(i%count,(i+1)%count));
    const previous = gl.draws.length; assert.equal(painter.draw(frame(nodes,edges)), true);
    const submitted = gl.draws.slice(previous); assert.equal(submitted.length,2);
    assert.equal(submitted[0].count, count*2*6); assert.equal(submitted[1].count,count);
  }
  assert.equal(gl.created.buffer.length,5, 'capacity growth never creates a new GPUBuffer');
  assert.equal(gl.allocations.length,4, 'two live batches grow once, then retain capacity when the graph shrinks');
  painter.dispose(); gl.assertReleased();
});

test('getError/getParameter run only on initialization and reconstruction, never on drawing, growth, resize or cleanup', () => {
  const { gl, canvas, painter } = setup();
  assert.deepEqual(gl.queries, { errors:1, parameters:3 });
  painter.resize(960,620,2);
  for (const count of [2,300,600,2,0]) {
    const nodes = Array.from({length:count}, (_, i) => node(`${i}`, {x:20+(i%30)*30,y:20+Math.floor(i/30)*25,labelOpacity:0}));
    const previous = gl.draws.length;
    assert.equal(painter.draw(frame(nodes,[])),true);
    assert.equal(painter.mode,'webgl');
    assert.equal(gl.draws.length,previous+(count ? 1 : 0),'verify actual submissions while forbidding synchronous queries');
    assert.deepEqual(gl.queries,{errors:1,parameters:3});
  }
  painter.resize(480,310,1);
  assert.equal(painter.draw(frame([],[],{x:NaN,y:0,k:1})),false);
  canvas.lose(); assert.equal(painter.draw(frame()),true);
  assert.deepEqual(gl.queries,{errors:1,parameters:3});
  canvas.restore(); assert.deepEqual(gl.queries,{errors:2,parameters:6});
  assert.equal(painter.draw(frame()),true); assert.equal(painter.mode,'webgl');
  painter.dispose(); gl.assertReleased();
  assert.deepEqual(gl.queries,{errors:2,parameters:6});
});

test('viewport culling keeps crossing edges, thick boundary edges, overlapping circles and offscreen halo fringes', () => {
  const { gl, painter } = setup(); painter.resize(960,620,1);
  const nodes = [node('left', { x:-100, y:200 }), node('right', { x:1100, y:200 }),
    node('far-a', { x:-100,y:-100 }), node('far-b', { x:-50,y:-50 }),
    node('fringe', { x:-4,y:100,radius:5 }), node('halo', { x:-10,y:150,radius:5,halo:1 }),
    node('top-a', { x:100,y:-4 }), node('top-b', { x:200,y:-4 })];
  assert.equal(painter.draw(frame(nodes,[edge(0,1),edge(2,3),edge(6,7,{width:10})])),true);
  const lines = typedDraw(gl,'a_shape')[0]; assert.equal(lines.count,12);
  assert.deepEqual(lines.attrs.a_position.slice(0,2), [[-100,201],[-100,199]]);
  const circles = typedDraw(gl,'a_shape').slice(1);
  assert.equal(circles[0].count,6); assert.equal(circles[0].attrs.a_shape[0][0],2, 'halo fringe emitted even when dot invisible');
  assert.equal(circles[1].count,18, 'left and two top fringes render as circle quads');
  assert.equal(typedDraw(gl,'a_size').length,0,'centers outside viewport must not use POINTS');
  painter.dispose(); gl.assertReleased();
});

test('halo batches precede dots, alpha is continuous, and point hardware limits use full-sized circle quads', () => {
  const { gl, painter } = setup({ gl: { maxPoint: 16 } }); painter.resize(960,620,2);
  const nodes = [node('small', { radius:3, halo:0.4, opacity:0.5 }), node('big', { x:200,radius:12 }),
    node('tiny', { x:300,radius:0.1 })];
  assert.equal(painter.draw(frame(nodes,[])),true);
  const [halo, dots, largeDots] = gl.draws;
  assert.equal(halo.mode,gl.TRIANGLES); assert.equal(halo.attrs.a_shape[0][0],2); close(halo.attrs.a_color[0][3],0.2);
  assert.equal(dots.mode,gl.POINTS); assert.equal(dots.count,1); assert.deepEqual(dots.attrs.a_size,[[12]]);
  assert.equal(largeDots.count,12); assert.deepEqual(largeDots.attrs.a_position[0],[188,88]);
  assert.ok(largeDots.attrs.a_shape.every(([kind]) => kind === 1));
  painter.dispose(); gl.assertReleased();
});

test('labels have independent opacity/visibility, fixed CSS font, Unicode-safe truncation and side placement', () => {
  const { gl, labels, painter } = setup(); painter.resize(960,620,2);
  const nodes = [node('transparent', { opacity:0,labelOpacity:0.3,label:'😀基金'.repeat(40) }),
    node('hidden', { labelOpacity:0 }), node('offscreen', { y:900 }),
    node('right', { x:950,label:'右侧' }), node('offscreen-dot', { x:-20,label:'还看得到标签' })];
  assert.equal(painter.draw(frame(nodes,[])),true);
  const visible = texts(labels); assert.equal(visible.length,3);
  assert.equal(visible[0].alpha,0.3); assert.ok(visible[0].text.endsWith('…'));
  assert.ok(Array.from(visible[0].text).length*12 <= 180);
  assert.equal(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(visible[0].text),false);
  assert.equal(visible[1].align,'right'); assert.equal(visible[1].x,938);
  assert.equal(visible[2].text,'还看得到标签'); assert.equal(visible[2].x,-8);
  const oldFont = visible[0].font;
  const zoomed = frame([node('zoom', { x:100,y:100 })],[],{x:0,y:0,k:2}); painter.draw(zoomed);
  assert.equal(texts(labels).at(-1).font,oldFont); assert.equal(texts(labels).at(-1).x,217);
  assert.equal(labels.context.globalAlpha,1); painter.dispose(); gl.assertReleased();
});

test('invalid indices/coordinates/widths and zero opacity cannot poison batches; empty frame really clears', () => {
  const { gl, labels, painter } = setup(); painter.resize(960,620,1);
  const nodes = [node('a'),node('b',{x:200}),node('NaN',{x:NaN}),node('infinity',{y:Infinity}),
    node('negative',{radius:-1}),node('zero',{radius:0,labelOpacity:0}),node('invisible',{opacity:0,labelOpacity:0}),
    node('bad-color',{color:'not-hex',labelOpacity:0})];
  const edges = [edge(0,1),edge(-1,1),edge(0,100),edge(0.5,1),edge(0,NaN),edge(0,2),edge(0,0),
    edge(0,1,{width:0}),edge(0,1,{width:-1}),edge(0,1,{width:Infinity}),edge(0,1,{opacity:0})];
  assert.equal(painter.draw(frame(nodes,edges)),true);
  assert.deepEqual(gl.draws.map(draw => draw.count),[6,2]);
  assert.equal(texts(labels).length,2);
  const draws = gl.draws.length, clears = gl.clears.length, textClears = labels.context.calls.filter(call => call.type==='clear').length;
  assert.equal(painter.draw(frame([],[])),true);
  assert.equal(gl.draws.length,draws); assert.equal(gl.clears.length,clears+1);
  assert.equal(labels.context.calls.filter(call => call.type==='clear').length,textClears+1);
  for (const camera of [{x:NaN,y:0,k:1},{x:0,y:Infinity,k:1},{x:0,y:0,k:0},{x:0,y:0,k:-1},{x:0,y:0,k:Infinity}])
    assert.equal(painter.draw(frame(nodes,edges,camera)),false);
  assert.equal(gl.draws.length,draws); painter.dispose(); gl.assertReleased();
});

test('DPR, zero/invalid sizes and hardware bitmap limits are bounded; identical resize does not reset canvases', () => {
  const { gl, canvas, labels, painter } = setup({gl:{maxBitmap:2048}});
  painter.resize(960,620,99); assert.deepEqual([canvas.width,canvas.height],[1920,1240]);
  const writes = canvas.sizes.length + labels.sizes.length;
  painter.resize(960,620,99); assert.equal(canvas.sizes.length+labels.sizes.length,writes);
  painter.resize(5000,2500,2); assert.deepEqual([canvas.width,canvas.height],[2048,1024]);
  assert.equal(painter.draw(frame()),true);
  for (const dpr of [NaN,Infinity,0,-1]) { painter.resize(960,620,dpr); assert.equal(canvas.width,960); }
  painter.resize(960,620,0.5); assert.deepEqual([canvas.width,canvas.height],[480,310]);
  for (const [width,height] of [[0,620],[-1,620],[960,0],[NaN,620],[960,Infinity]]) {
    painter.resize(width,height,2); const calls = gl.draws.length;
    assert.equal(painter.draw(frame()),false); assert.equal(gl.draws.length,calls);
  }
  painter.resize(960,620,1); assert.equal(painter.draw(frame()),true);
  painter.dispose(); gl.assertReleased();
});

test('WebGL null/throw falls back to primary Canvas2D with geometry and independent labels', () => {
  for (const throwWebGL of [false,true]) {
    const { canvas, labels, painter } = setup({noWebGL:true,canvas:{throwWebGL}});
    assert.equal(painter.mode,'canvas2d'); painter.resize(1200,620,2);
    assert.equal(painter.draw(frame([node('a',{halo:0.5}),node('b',{x:200})])),true);
    const calls = canvas.context.calls, strokes = calls.filter(call => call.type==='stroke'), fills = calls.filter(call => call.type==='fill');
    assert.equal(strokes.length,1); assert.equal(strokes[0].width,2); assert.equal(strokes[0].alpha,0.4);
    assert.deepEqual(strokes[0].path[0].values,[220,100]);
    assert.equal(fills.length,3); close(fills[0].alpha,0.14); assert.equal(fills[1].alpha,1);
    assert.equal(calls.some(call => call.type==='text'),false); assert.equal(texts(labels).length,2);
    assert.equal(labels.context.calls.some(call => call.type==='fill'),false);
    painter.dispose(); assert.equal(canvas.listeners.size,0);
    assert.equal(canvas.context.calls.at(-1).type,'clear'); assert.equal(labels.context.calls.at(-1).type,'clear');
  }
});

test('all partial shader/program/buffer setup failures delete their allocations and render via the locked-canvas overlay', () => {
  for (const options of [{failShaderAt:1},{failShaderAt:2},{failCompileAt:1},{failCompileAt:4},
    {failProgramAt:1},{failProgramAt:2},{failLinkAt:1},{failLinkAt:2},{failBufferAt:1},{failBufferAt:4},
    {missingUniform:true},{initializationError:0x0505}]) {
    const { gl, canvas, labels, painter } = setup({gl:options});
    assert.equal(painter.mode,'canvas2d',JSON.stringify(options)); gl.assertReleased();
    painter.resize(960,620,1); assert.equal(painter.draw(frame()),true);
    assert.equal(gl.draws.length,0); assert.equal(labels.context.calls.filter(call => call.type==='fill').length,2);
    assert.equal(texts(labels).length,2);
    assert.equal(canvas.requests.some(request => request.kind==='2d'),false,'never pretend a WebGL canvas can switch context kind');
    painter.dispose(); gl.assertReleased();
  }
});

test('loss immediately invalidates, fallback remains drawable, restore recreates GPU state and reuses CPU buffers', () => {
  const { gl, canvas, labels, painter, state } = setup(); painter.resize(960,620,2); painter.draw(frame());
  const oldBuffers = gl.created.buffer.slice(), oldPrograms = gl.created.program.slice(), oldUploads = gl.uploads.slice();
  const event = canvas.lose(); assert.equal(event.defaultPrevented,true); assert.equal(state.invalidations,1);
  assert.equal(painter.mode,'canvas2d'); gl.assertReleased();
  const previousDraws = gl.draws.length;
  assert.equal(painter.draw(frame()),true); assert.equal(gl.draws.length,previousDraws);
  assert.equal(labels.context.calls.filter(call => call.type==='fill').length,2);
  painter.resize(1200,620,2); canvas.restore();
  assert.equal(painter.mode,'webgl'); assert.equal(state.invalidations,2);
  assert.equal(gl.created.buffer.length,10); assert.equal(gl.created.program.length,4);
  assert.ok(gl.created.buffer.slice(5).every(buffer => !oldBuffers.includes(buffer)));
  assert.ok(gl.created.program.slice(2).every(program => !oldPrograms.includes(program)));
  assert.deepEqual(gl.viewports.at(-1),[0,0,2400,1240]);
  const fillCount = labels.context.calls.filter(call => call.type==='fill').length;
  assert.equal(painter.draw(frame()),true);
  assert.deepEqual(gl.draws.at(-1).attrs.a_position[0],[220,100]);
  assert.equal(labels.context.calls.filter(call => call.type==='fill').length,fillCount);
  gl.uploads.slice(-2).forEach((upload,i) => { assert.equal(upload.source,oldUploads[i].source); assert.notEqual(upload.buffer,oldUploads[i].buffer); });
  painter.dispose(); gl.assertReleased();
  canvas.dispatchEvent(new Event('webglcontextrestored')); assert.equal(state.invalidations,2);
});

test('restore failure keeps 2D usable and later context restoration can recover again', () => {
  const { gl, canvas, painter, state } = setup(); painter.resize(960,620,1); painter.draw(frame()); canvas.lose();
  gl.options.failCompileAt = 5; canvas.restore(); assert.equal(painter.mode,'canvas2d');
  assert.equal(painter.draw(frame()),true); gl.assertReleased();
  canvas.lose(); canvas.restore(); assert.equal(painter.mode,'webgl'); assert.equal(painter.draw(frame()),true);
  assert.equal(state.invalidations,4); painter.dispose(); gl.assertReleased();
});

test('command exceptions and context loss submit a real fallback frame, never count failed WebGL alone', () => {
  for (const failure of ['throwOnDraw','loseDuringDraw','lost']) {
    const { gl, labels, painter } = setup(); painter.resize(960,620,1);
    gl[failure] = true;
    assert.equal(painter.draw(frame()),true,`${failure} has a successful 2D fallback submission`);
    assert.equal(painter.mode,'canvas2d'); assert.equal(labels.context.calls.filter(call => call.type==='stroke').length,1);
    assert.equal(labels.context.calls.filter(call => call.type==='fill').length,2); painter.dispose(); gl.assertReleased();
  }
  for (const failure of ['throwOnDraw','loseDuringDraw','lost']) {
    const { gl, painter } = setup({labels:{no2d:true}}); painter.resize(960,620,1); gl[failure] = true;
    // One point batch makes loss happen after the last successful draw call:
    // no later API exception can accidentally stand in for the lost-state check.
    assert.equal(painter.draw(frame([node('only')],[])),false,`${failure} without a drawable fallback must not count a submitted frame`);
    painter.dispose(); gl.assertReleased();
  }
});

test('the reconstruction-only error query still rejects failed GPU setup and allows a later recovery', () => {
  const { gl, canvas, painter } = setup(); painter.resize(960,620,1); painter.draw(frame());
  canvas.lose(); gl.error = 0x0505; canvas.restore();
  assert.equal(gl.queries.errors,2); assert.equal(painter.mode,'canvas2d'); gl.assertReleased();
  assert.equal(painter.draw(frame()),true); assert.equal(gl.queries.errors,2);
  canvas.lose(); canvas.restore();
  assert.equal(gl.queries.errors,3); assert.equal(painter.mode,'webgl'); assert.equal(painter.draw(frame()),true);
  painter.dispose(); gl.assertReleased();
});

test('unavailable contexts and failed labels do not report completion; dispose is idempotent and all later calls are inert', () => {
  const unavailable = setup({noWebGL:true,canvas:{no2d:true},labels:{no2d:true}});
  unavailable.painter.resize(960,620,1); assert.equal(unavailable.painter.draw(frame()),false); unavailable.painter.dispose();
  const { gl, canvas, labels, painter, state } = setup(); painter.resize(960,620,1);
  labels.context.throwText = true; assert.equal(painter.draw(frame()),false); labels.context.throwText = false;
  assert.equal(painter.draw(frame()),true); painter.dispose(); gl.assertReleased();
  const draws = gl.draws.length, created = structuredClone(gl.attempts), canvasWrites = canvas.sizes.length, labelCalls = labels.context.calls.length;
  const savedInvalidations = state.invalidations;
  painter.dispose(); painter.resize(200,200,2); assert.equal(painter.draw(frame()),false);
  canvas.dispatchEvent(new Event('webglcontextlost',{cancelable:true})); canvas.dispatchEvent(new Event('webglcontextrestored'));
  assert.equal(canvas.listeners.size,0); assert.equal(gl.draws.length,draws); assert.deepEqual(gl.attempts,created);
  assert.equal(canvas.sizes.length,canvasWrites); assert.equal(labels.context.calls.length,labelCalls); assert.equal(state.invalidations,savedInvalidations);
  assert.equal(labels.context.calls.at(-1).type,'clear');
});

test('disposing while context is lost releases listeners without restoring or scheduling any work', () => {
  const { gl, canvas, painter, state } = setup(); painter.resize(960,620,1); painter.draw(frame()); canvas.lose();
  painter.dispose(); gl.assertReleased(); assert.equal(canvas.listeners.size,0);
  const attempts = structuredClone(gl.attempts); canvas.restore(); assert.deepEqual(gl.attempts,attempts);
  assert.equal(state.invalidations,1); assert.equal(painter.draw(frame()),false);
});
