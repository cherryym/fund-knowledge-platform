/** v8 rendering only: no animation clock, graph state, UI or dependencies. */
export type StarPaintNode = {
  id: string; x: number; y: number; radius: number; color: string;
  opacity: number; halo: number; label: string; labelOpacity: number;
};
export type StarPaintEdge = {
  source: number; target: number; color: string; opacity: number; width: number;
};
export type StarPaintFrame = {
  nodes: StarPaintNode[]; edges: StarPaintEdge[]; camera: { x: number; y: number; k: number };
};

const WORLD_WIDTH = 960, WORLD_HEIGHT = 620;
const EMPTY = new Float32Array(0), EMPTY_POSITIONS = new Float64Array(0);
const POINT_STRIDE = 8, QUAD_STRIDE = 10, HALO_RADIUS = 2.7;
const LABEL_FONT = '12px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
const MAX_LABEL_WIDTH = 180, CACHE_LIMIT = 512;
const unit = (value: number) => Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0;
// Leave enough headroom for quad arithmetic before conversion to Float32.
const finite = (value: number) => Number.isFinite(value) && Math.abs(value) < 1e35;

const POINT_VERTEX = `
attribute vec2 a_position;
attribute float a_size;
attribute vec4 a_color;
attribute float a_kind;
uniform vec2 u_view;
varying lowp vec4 v_color;
varying mediump float v_kind;
varying mediump float v_feather;
void main() {
  gl_Position = vec4(a_position / u_view * vec2(2.0, -2.0) + vec2(-1.0, 1.0), 0.0, 1.0);
  gl_PointSize = a_size;
  v_color = a_color;
  v_kind = a_kind;
  v_feather = min(0.5, 2.0 / max(a_size, 1.0));
}`;
const POINT_FRAGMENT = `
precision mediump float;
varying lowp vec4 v_color;
varying mediump float v_kind;
varying mediump float v_feather;
void main() {
  float d = length(gl_PointCoord * 2.0 - 1.0);
  if (d >= 1.0) discard;
  float coverage = 1.0 - smoothstep(1.0 - v_feather, 1.0, d);
  if (v_kind > 0.5) coverage = 0.28 * pow(1.0 - d * d, 2.0);
  float alpha = v_color.a * coverage;
  gl_FragColor = vec4(v_color.rgb * alpha, alpha);
}`;
const QUAD_VERTEX = `
attribute vec2 a_position;
attribute vec2 a_local;
attribute vec4 a_color;
attribute vec2 a_shape;
uniform vec2 u_view;
varying mediump vec2 v_local;
varying lowp vec4 v_color;
varying mediump vec2 v_shape;
void main() {
  gl_Position = vec4(a_position / u_view * vec2(2.0, -2.0) + vec2(-1.0, 1.0), 0.0, 1.0);
  v_local = a_local;
  v_color = a_color;
  v_shape = a_shape;
}`;
const QUAD_FRAGMENT = `
precision mediump float;
varying mediump vec2 v_local;
varying lowp vec4 v_color;
varying mediump vec2 v_shape;
void main() {
  float coverage = 1.0;
  if (v_shape.x > 0.5) {
    float d = length(v_local);
    if (d >= 1.0) discard;
    coverage = 1.0 - smoothstep(1.0 - v_shape.y, 1.0, d);
    if (v_shape.x > 1.5) coverage = 0.28 * pow(1.0 - d * d, 2.0);
  }
  float alpha = v_color.a * coverage;
  gl_FragColor = vec4(v_color.rgb * alpha, alpha);
}`;

type Color = readonly [number, number, number, number];
type Program = { handle: WebGLProgram; view: WebGLUniformLocation };

class Batch {
  data = EMPTY;
  used = 0;
  buffer: WebGLBuffer | null = null;
  gpuBytes = 0;
  constructor(readonly stride: number) {}
  reserve(floats: number) {
    if (this.used + floats <= this.data.length) return;
    let capacity = Math.max(256, this.data.length);
    while (capacity < this.used + floats) capacity *= 2;
    const next = new Float32Array(capacity);
    next.set(this.data);
    this.data = next;
  }
  point(x: number, y: number, size: number, color: Color, opacity: number, halo: boolean) {
    this.reserve(POINT_STRIDE);
    let i = this.used;
    this.data[i++] = x; this.data[i++] = y; this.data[i++] = size;
    this.data[i++] = color[0]; this.data[i++] = color[1]; this.data[i++] = color[2];
    this.data[i++] = color[3] * opacity; this.data[i++] = halo ? 1 : 0;
    this.used = i;
  }
  vertex(x: number, y: number, u: number, v: number, color: Color, opacity: number, kind: number, feather: number) {
    let i = this.used;
    this.data[i++] = x; this.data[i++] = y; this.data[i++] = u; this.data[i++] = v;
    this.data[i++] = color[0]; this.data[i++] = color[1]; this.data[i++] = color[2];
    this.data[i++] = color[3] * opacity; this.data[i++] = kind; this.data[i++] = feather;
    this.used = i;
  }
  circle(x: number, y: number, r: number, color: Color, opacity: number, halo: boolean, dpr: number) {
    this.reserve(6 * QUAD_STRIDE);
    const kind = halo ? 2 : 1, feather = Math.min(0.5, 1 / (r * dpr));
    this.vertex(x-r, y-r, -1, -1, color, opacity, kind, feather);
    this.vertex(x+r, y-r, 1, -1, color, opacity, kind, feather);
    this.vertex(x+r, y+r, 1, 1, color, opacity, kind, feather);
    this.vertex(x-r, y-r, -1, -1, color, opacity, kind, feather);
    this.vertex(x+r, y+r, 1, 1, color, opacity, kind, feather);
    this.vertex(x-r, y+r, -1, 1, color, opacity, kind, feather);
  }
  edge(ax: number, ay: number, bx: number, by: number, half: number, color: Color, opacity: number) {
    const length = Math.hypot(bx-ax, by-ay);
    if (!length) return;
    const nx = -(by-ay) / length * half, ny = (bx-ax) / length * half;
    this.reserve(6 * QUAD_STRIDE);
    this.vertex(ax+nx, ay+ny, 0, 0, color, opacity, 0, 0);
    this.vertex(ax-nx, ay-ny, 0, 0, color, opacity, 0, 0);
    this.vertex(bx-nx, by-ny, 0, 0, color, opacity, 0, 0);
    this.vertex(ax+nx, ay+ny, 0, 0, color, opacity, 0, 0);
    this.vertex(bx-nx, by-ny, 0, 0, color, opacity, 0, 0);
    this.vertex(bx+nx, by+ny, 0, 0, color, opacity, 0, 0);
  }
}

function program(gl: WebGLRenderingContext, vertex: string, fragment: string, names: string[]): Program {
  const shaders: WebGLShader[] = [];
  let handle: WebGLProgram | null = null;
  try {
    for (const [type, source] of [[gl.VERTEX_SHADER, vertex], [gl.FRAGMENT_SHADER, fragment]] as const) {
      const shader = gl.createShader(type);
      if (!shader) throw new Error('WebGL shader allocation failed');
      shaders.push(shader);
      gl.shaderSource(shader, source); gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error('WebGL shader compilation failed');
    }
    handle = gl.createProgram();
    if (!handle) throw new Error('WebGL program allocation failed');
    for (const shader of shaders) gl.attachShader(handle, shader);
    names.forEach((name, index) => gl.bindAttribLocation(handle!, index, name));
    gl.linkProgram(handle);
    if (!gl.getProgramParameter(handle, gl.LINK_STATUS)) throw new Error('WebGL program linking failed');
    const view = gl.getUniformLocation(handle, 'u_view');
    if (view === null) throw new Error('WebGL viewport uniform unavailable');
    for (const shader of shaders) gl.detachShader(handle, shader);
    return { handle, view };
  } catch (error) {
    if (handle) gl.deleteProgram(handle);
    throw error;
  } finally {
    for (const shader of shaders) gl.deleteShader(shader);
  }
}

// Segment/slab intersection also retains edges crossing the viewport with BOTH
// endpoints outside. Expanding by half-width retains thick boundary strokes.
function edgeVisible(ax: number, ay: number, bx: number, by: number, half: number, width: number, height: number) {
  let start = 0, end = 1;
  const dx = bx-ax, dy = by-ay;
  if (!dx) { if (ax < -half || ax > width+half) return false; }
  else {
    const a = (-half-ax)/dx, b = (width+half-ax)/dx;
    start = Math.max(start, Math.min(a, b)); end = Math.min(end, Math.max(a, b));
  }
  if (!dy) { if (ay < -half || ay > height+half) return false; }
  else {
    const a = (-half-ay)/dy, b = (height+half-ay)/dy;
    start = Math.max(start, Math.min(a, b)); end = Math.min(end, Math.max(a, b));
  }
  return start <= end && (dx !== 0 || dy !== 0);
}

/**
 * Owns the two supplied drawing surfaces, but never their CSS layout or events
 * for interaction. resize() takes CSS dimensions. draw() means drawing calls
 * returned without throwing and the context stayed live (including clearing an
 * empty frame). It does not query GPU errors, completion or presentation/FPS.
 *
 * A canvas already bound to WebGL cannot acquire a 2D context. On context loss
 * or shader failure, the existing 2D overlay temporarily paints geometry before
 * its independent text pass. No extra DOM layer or replacement canvas is needed.
 */
export function createStarPainter(canvas: HTMLCanvasElement, labels: HTMLCanvasElement, onInvalidate?: () => void): {
  readonly mode: 'webgl' | 'canvas2d';
  resize(width: number, height: number, dpr: number): void;
  draw(frame: StarPaintFrame): boolean;
  dispose(): void;
} {
  let surface: HTMLCanvasElement | null = canvas, overlay: HTMLCanvasElement | null = labels;
  let invalidate = onInvalidate;
  let gl: WebGLRenderingContext | null = null;
  let textContext: CanvasRenderingContext2D | null = null, fallback: CanvasRenderingContext2D | null = null;
  let points: Program | null = null, quads: Program | null = null;
  let disposed = false, lost = false;
  let width = 0, height = 0, requestedDpr = 1, pixelRatio = 1;
  let maxBitmap = 8192, minPoint = 1, maxPoint = 1;
  let positions = EMPTY_POSITIONS;
  const edges = new Batch(QUAD_STRIDE), halos = new Batch(POINT_STRIDE), haloQuads = new Batch(QUAD_STRIDE);
  const dots = new Batch(POINT_STRIDE), dotQuads = new Batch(QUAD_STRIDE);
  const batches = [edges, halos, haloQuads, dots, dotQuads];
  const colors = new Map<string, Color>();
  const texts = new Map<string, { text: string; width: number }>();

  function color(value: string): Color {
    const cached = colors.get(value);
    if (cached) return cached;
    // The public contract supplies validated hex; guard invalid JS callers too.
    let hex = /^#(?:[\da-f]{3,4}|[\da-f]{6}|[\da-f]{8})$/i.test(value) ? value.slice(1) : '00000000';
    if (hex.length <= 4) hex = [...hex].map(part => part+part).join('');
    const rgba: Color = [parseInt(hex.slice(0, 2), 16)/255, parseInt(hex.slice(2, 4), 16)/255,
      parseInt(hex.slice(4, 6), 16)/255, hex.length === 8 ? parseInt(hex.slice(6, 8), 16)/255 : 1];
    if (colors.size >= CACHE_LIMIT) colors.clear();
    colors.set(value, rgba);
    return rgba;
  }
  function clear2d(context: CanvasRenderingContext2D | null) {
    if (!context) return;
    context.setTransform(1, 0, 0, 1, 0, 0);
    context.globalAlpha = 1;
    context.clearRect(0, 0, context.canvas.width, context.canvas.height);
  }
  function prepare2d(context: CanvasRenderingContext2D) {
    context.setTransform(context.canvas.width / width, 0, 0, context.canvas.height / height, 0, 0);
    context.globalAlpha = 1;
    context.globalCompositeOperation = 'source-over';
  }
  function releaseGPU() {
    if (gl) {
      gl.bindBuffer(gl.ARRAY_BUFFER, null); gl.useProgram(null);
      for (let index = 0; index < 4; index++) gl.disableVertexAttribArray(index);
      for (const batch of batches) if (batch.buffer) gl.deleteBuffer(batch.buffer);
      if (points) gl.deleteProgram(points.handle);
      if (quads) gl.deleteProgram(quads.handle);
    }
    points = quads = null;
    for (const batch of batches) { batch.buffer = null; batch.gpuBytes = 0; }
  }
  function initializeGPU() {
    if (!gl || gl.isContextLost()) return false;
    try {
      points = program(gl, POINT_VERTEX, POINT_FRAGMENT, ['a_position', 'a_size', 'a_color', 'a_kind']);
      quads = program(gl, QUAD_VERTEX, QUAD_FRAGMENT, ['a_position', 'a_local', 'a_color', 'a_shape']);
      for (const batch of batches) {
        batch.buffer = gl.createBuffer(); batch.gpuBytes = 0;
        if (!batch.buffer) throw new Error('WebGL buffer allocation failed');
      }
      const range = gl.getParameter(gl.ALIASED_POINT_SIZE_RANGE) as Float32Array;
      minPoint = Math.max(1, range[0]); maxPoint = Math.max(minPoint, range[1]);
      const viewport = gl.getParameter(gl.MAX_VIEWPORT_DIMS) as Int32Array;
      maxBitmap = Math.max(1, Math.min(8192, viewport[0], viewport[1], gl.getParameter(gl.MAX_RENDERBUFFER_SIZE)));
      gl.disable(gl.DEPTH_TEST); gl.disable(gl.CULL_FACE); gl.disable(gl.SCISSOR_TEST);
      gl.enable(gl.BLEND); gl.blendEquation(gl.FUNC_ADD);
      // Shaders emit premultiplied color, matching the browser compositor.
      gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.clearColor(0, 0, 0, 0);
      // Synchronous error queries belong only to initialization/reconstruction.
      if (gl.getError() !== gl.NO_ERROR || gl.isContextLost()) throw new Error('WebGL initialization failed');
      return true;
    } catch {
      releaseGPU();
      return false;
    }
  }
  function useFallback() {
    if (!fallback) {
      // Only ask for 2D on the main surface if WebGL never claimed it.
      if (!gl) { try { fallback = surface?.getContext('2d') ?? null; } catch { /* unavailable */ } }
      if (!fallback) fallback = textContext;
    }
  }
  function resize(nextWidth: number, nextHeight: number, dpr: number) {
    if (disposed || !surface || !overlay) return;
    width = finite(nextWidth) && nextWidth > 0 ? nextWidth : 0;
    height = finite(nextHeight) && nextHeight > 0 ? nextHeight : 0;
    requestedDpr = Number.isFinite(dpr) && dpr > 0 ? Math.min(2, dpr) : 1;
    const ratio = Math.min(requestedDpr, maxBitmap / Math.max(1, width), maxBitmap / Math.max(1, height));
    const bitmapWidth = width ? Math.max(1, Math.round(width * ratio)) : 0;
    const bitmapHeight = height ? Math.max(1, Math.round(height * ratio)) : 0;
    // Avoid resetting contexts on identical resizes (e.g. ResizeObserver).
    for (const target of [surface, overlay]) {
      if (target.width !== bitmapWidth) target.width = bitmapWidth;
      if (target.height !== bitmapHeight) target.height = bitmapHeight;
    }
    pixelRatio = width && height ? Math.min(bitmapWidth/width, bitmapHeight/height) : ratio;
    if (gl && points && !lost) gl.viewport(0, 0, bitmapWidth, bitmapHeight);
  }
  function onLost(event: Event) {
    if (disposed) return;
    event.preventDefault(); // Required by WebGL for restoration to be attempted.
    lost = true;
    releaseGPU();
    useFallback();
    clear2d(textContext);
    invalidate?.();
  }
  function onRestored() {
    if (disposed) return;
    lost = false;
    releaseGPU();
    if (initializeGPU()) {
      fallback = null;
      resize(width, height, requestedDpr);
      clear2d(textContext);
    } else useFallback();
    invalidate?.();
  }

  try { textContext = labels.getContext('2d'); } catch { /* drawing reports failure if unavailable */ }
  try {
    gl = canvas.getContext('webgl', { alpha: true, premultipliedAlpha: true, antialias: true, depth: false, stencil: false });
  } catch { /* disabled or unavailable WebGL */ }
  canvas.addEventListener('webglcontextlost', onLost);
  canvas.addEventListener('webglcontextrestored', onRestored);
  if (!initializeGPU()) useFallback();

  function project(frame: StarPaintFrame) {
    if (positions.length < frame.nodes.length * 3) {
      let capacity = Math.max(192, positions.length);
      while (capacity < frame.nodes.length * 3) capacity *= 2;
      positions = new Float64Array(capacity);
    }
    const fit = Math.min(width/WORLD_WIDTH, height/WORLD_HEIGHT);
    const scale = fit * frame.camera.k;
    const x = (width-WORLD_WIDTH*fit)/2 + fit*frame.camera.x;
    const y = (height-WORLD_HEIGHT*fit)/2 + fit*frame.camera.y;
    for (let i = 0; i < frame.nodes.length; i++) {
      const node = frame.nodes[i], offset = i*3;
      positions[offset] = positions[offset+1] = positions[offset+2] = NaN;
      if (!node || !finite(node.x) || !finite(node.y) || !finite(node.radius) || node.radius < 0) continue;
      const px = x + node.x*scale, py = y + node.y*scale, radius = node.radius*scale;
      if (!finite(px) || !finite(py) || !finite(radius)) continue;
      positions[offset] = px; positions[offset+1] = py; positions[offset+2] = radius;
    }
    return scale;
  }
  function circleVisible(x: number, y: number, r: number) {
    return x+r >= 0 && y+r >= 0 && x-r <= width && y-r <= height;
  }
  function addCircle(x: number, y: number, r: number, rgba: Color, opacity: number, halo: boolean) {
    if (!circleVisible(x, y, r)) return;
    const size = r * 2 * pixelRatio;
    // POINTS clip by their center and have an implementation-specific size cap.
    // Quads preserve partially visible and oversized circles without clamping.
    if (x >= 0 && x <= width && y >= 0 && y <= height && size >= minPoint && size <= maxPoint) {
      (halo ? halos : dots).point(x, y, size, rgba, opacity, halo);
    } else (halo ? haloQuads : dotQuads).circle(x, y, r, rgba, opacity, halo, pixelRatio);
  }
  function validEdge(edge: StarPaintEdge, count: number, scale: number) {
    if (!edge || !Number.isInteger(edge.source) || !Number.isInteger(edge.target)
      || edge.source < 0 || edge.target < 0 || edge.source >= count || edge.target >= count
      || !finite(edge.width) || edge.width <= 0 || !unit(edge.opacity)) return false;
    const a = edge.source*3, b = edge.target*3, half = edge.width*scale/2;
    return finite(positions[a]) && finite(positions[b]) && finite(half)
      && edgeVisible(positions[a], positions[a+1], positions[b], positions[b+1], half, width, height);
  }
  function fillBatches(frame: StarPaintFrame, scale: number) {
    for (const batch of batches) batch.used = 0;
    for (const edge of frame.edges) {
      if (!validEdge(edge, frame.nodes.length, scale)) continue;
      const rgba = color(edge.color);
      if (!rgba[3]) continue;
      const a = edge.source*3, b = edge.target*3;
      edges.edge(positions[a], positions[a+1], positions[b], positions[b+1], edge.width*scale/2, rgba, unit(edge.opacity));
    }
    for (let i = 0; i < frame.nodes.length; i++) {
      const node = frame.nodes[i], offset = i*3;
      const x = positions[offset], y = positions[offset+1], radius = positions[offset+2];
      if (!finite(x) || !radius || !unit(node.opacity)) continue;
      const rgba = color(node.color);
      if (!rgba[3]) continue;
      if (unit(node.halo)) addCircle(x, y, radius*HALO_RADIUS, rgba, unit(node.opacity)*unit(node.halo), true);
      addCircle(x, y, radius, rgba, unit(node.opacity), false);
    }
  }
  function submit(batch: Batch, shader: Program) {
    if (!gl || !batch.used || !batch.buffer) return;
    gl.useProgram(shader.handle); gl.uniform2f(shader.view, width, height);
    gl.bindBuffer(gl.ARRAY_BUFFER, batch.buffer);
    if (batch.gpuBytes < batch.data.byteLength) {
      gl.bufferData(gl.ARRAY_BUFFER, batch.data.byteLength, gl.DYNAMIC_DRAW);
      batch.gpuBytes = batch.data.byteLength;
    }
    // Upload the reusable capacity view. No per-frame subarray/view allocation;
    // spare capacity never renders because drawArrays uses the live vertex count.
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, batch.data);
    for (let index = 0; index < 4; index++) gl.enableVertexAttribArray(index);
    const stride = batch.stride * 4;
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, stride, 0);
    if (batch.stride === POINT_STRIDE) {
      gl.vertexAttribPointer(1, 1, gl.FLOAT, false, stride, 8);
      gl.vertexAttribPointer(2, 4, gl.FLOAT, false, stride, 12);
      gl.vertexAttribPointer(3, 1, gl.FLOAT, false, stride, 28);
    } else {
      gl.vertexAttribPointer(1, 2, gl.FLOAT, false, stride, 8);
      gl.vertexAttribPointer(2, 4, gl.FLOAT, false, stride, 16);
      gl.vertexAttribPointer(3, 2, gl.FLOAT, false, stride, 32);
    }
    gl.drawArrays(batch.stride === POINT_STRIDE ? gl.POINTS : gl.TRIANGLES, 0, batch.used/batch.stride);
  }
  function draw2d(frame: StarPaintFrame, scale: number) {
    if (!fallback) return false;
    const context = fallback;
    if (context !== textContext) clear2d(context);
    prepare2d(context);
    context.lineCap = 'butt';
    for (const edge of frame.edges) {
      if (!validEdge(edge, frame.nodes.length, scale) || !color(edge.color)[3]) continue;
      const a = edge.source*3, b = edge.target*3;
      context.globalAlpha = unit(edge.opacity); context.strokeStyle = edge.color;
      context.lineWidth = edge.width*scale;
      context.beginPath(); context.moveTo(positions[a], positions[a+1]); context.lineTo(positions[b], positions[b+1]); context.stroke();
    }
    // All halos are behind all solid dots, as in the GPU batches.
    for (let pass = 0; pass < 2; pass++) for (let i = 0; i < frame.nodes.length; i++) {
      const node = frame.nodes[i], offset = i*3, halo = pass === 0;
      const x = positions[offset], y = positions[offset+1], r = positions[offset+2] * (halo ? HALO_RADIUS : 1);
      if (!finite(x) || !r || !unit(node.opacity) || (halo && !unit(node.halo))
        || !circleVisible(x, y, r) || !color(node.color)[3]) continue;
      context.globalAlpha = unit(node.opacity) * (halo ? unit(node.halo)*0.28 : 1);
      if (halo) {
        const gradient = context.createRadialGradient(x, y, 0, x, y, r);
        gradient.addColorStop(0, node.color); gradient.addColorStop(1, 'transparent');
        context.fillStyle = gradient;
      } else context.fillStyle = node.color;
      context.beginPath(); context.arc(x, y, r, 0, Math.PI*2); context.fill();
    }
    context.globalAlpha = 1;
    return true;
  }
  function labelText(label: string) {
    const cached = texts.get(label);
    if (cached) return cached;
    // Bound measurement work and never split a UTF-16 surrogate pair.
    const chars: string[] = [];
    for (const char of label) { if (chars.length === 48) break; chars.push(char); }
    let text = chars.join('');
    if (text.length < label.length) text += '…';
    let measured = textContext!.measureText(text).width;
    if (measured > MAX_LABEL_WIDTH) {
      let low = 0, high = chars.length;
      while (low < high) {
        const mid = Math.ceil((low+high)/2);
        if (textContext!.measureText(chars.slice(0, mid).join('')+'…').width <= MAX_LABEL_WIDTH) low = mid;
        else high = mid-1;
      }
      text = chars.slice(0, low).join('')+'…'; measured = textContext!.measureText(text).width;
    }
    const result = { text, width: measured };
    if (texts.size >= CACHE_LIMIT) texts.clear();
    texts.set(label, result);
    return result;
  }
  function drawLabels(frame: StarPaintFrame) {
    if (!textContext) return;
    const context = textContext;
    prepare2d(context);
    context.font = LABEL_FONT; context.textBaseline = 'middle';
    for (let i = 0; i < frame.nodes.length; i++) {
      const node = frame.nodes[i], offset = i*3, x = positions[offset], y = positions[offset+1], r = positions[offset+2];
      if (!finite(x) || !node.label || !unit(node.labelOpacity) || y+8 < 0 || y-8 > height) continue;
      // Choose the side with more room; cull text by its own bounds, independent
      // of node opacity and dot visibility. Fixed CSS font size survives zoom.
      const left = x > width/2, anchor = x + (left ? -1 : 1)*(r+7);
      if ((!left && (anchor > width || anchor+MAX_LABEL_WIDTH < 0))
        || (left && (anchor < 0 || anchor-MAX_LABEL_WIDTH > width))) continue;
      const label = labelText(node.label);
      if ((!left && anchor+label.width < 0) || (left && anchor-label.width > width) || !color(node.color)[3]) continue;
      context.textAlign = left ? 'right' : 'left'; context.fillStyle = node.color;
      context.globalAlpha = unit(node.labelOpacity);
      context.fillText(label.text, anchor, y);
    }
    context.globalAlpha = 1;
  }
  function draw(frame: StarPaintFrame) {
    if (disposed || !surface || !overlay || width <= 0 || height <= 0) return false;
    clear2d(textContext);
    const camera = frame.camera;
    if (!finite(camera.x) || !finite(camera.y) || !finite(camera.k) || camera.k <= 0) {
      if (gl && points && !gl.isContextLost()) gl.clear(gl.COLOR_BUFFER_BIT);
      if (fallback !== textContext) clear2d(fallback);
      return false;
    }
    const scale = project(frame);
    let submitted = false;
    if (gl && points && quads && !lost && !gl.isContextLost()) {
      try {
        fillBatches(frame, scale);
        gl.viewport(0, 0, surface.width, surface.height); gl.clear(gl.COLOR_BUFFER_BIT);
        submit(edges, quads); submit(halos, points); submit(haloQuads, quads);
        submit(dots, points); submit(dotQuads, quads);
        // Calls returned without throwing; verify the context stayed live.
        // No synchronous GPU error/completion query in the production frame.
        // Submission is not proof of rasterization or browser presentation.
        submitted = !gl.isContextLost();
      } catch { /* retry this actual frame on Canvas2D below */ }
      if (!submitted) {
        if (!gl.isContextLost()) gl.clear(gl.COLOR_BUFFER_BIT);
        releaseGPU(); useFallback();
      }
    } else if (!fallback) { releaseGPU(); useFallback(); }
    try {
      if (!submitted) submitted = draw2d(frame, scale);
      drawLabels(frame);
      return submitted;
    } catch {
      // A lost/unavailable 2D overlay cannot be counted as a completed frame.
      return false;
    }
  }
  function dispose() {
    if (disposed) return;
    disposed = true;
    surface?.removeEventListener('webglcontextlost', onLost);
    surface?.removeEventListener('webglcontextrestored', onRestored);
    if (gl && !gl.isContextLost()) gl.clear(gl.COLOR_BUFFER_BIT);
    releaseGPU();
    clear2d(textContext);
    if (fallback !== textContext) clear2d(fallback);
    for (const batch of batches) { batch.data = EMPTY; batch.used = 0; }
    positions = EMPTY_POSITIONS; colors.clear(); texts.clear();
    gl = null; textContext = fallback = null; surface = overlay = null; invalidate = undefined;
  }
  return { get mode() { return points && !lost ? 'webgl' : 'canvas2d'; }, resize, draw, dispose };
}
