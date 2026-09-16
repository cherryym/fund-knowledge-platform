/** View-only height preference. Resizing never updates graph data or restarts physics. */
export function graphHeightLimits(width: number, height: number) {
  const viewport = Number.isFinite(height) && height > 0 ? height : 900;
  const narrow = Number.isFinite(width) && width <= 560;
  return { min: narrow ? 280 : 400, max: Math.round(Math.max(1200, Math.min(2400, viewport * 2))),
    defaultHeight: Math.round(Math.min(narrow ? 680 : 1100, Math.max(narrow ? 440 : 680, viewport * (narrow ? .7 : .78)))) };
}
export function clampGraphHeight(value: number, limits: ReturnType<typeof graphHeightLimits>) {
  return Math.round(Math.min(limits.max, Math.max(limits.min, Number.isFinite(value) ? value : limits.defaultHeight)));
}

export function attachGraphHeightResize(handle: HTMLElement, stage: HTMLElement, preferenceKey?: string) {
  const property = "--star-stage-height";
  const storageKey = preferenceKey ? `fkb:ui:graph-height:v1:${encodeURIComponent(preferenceKey)}` : null;
  const original = stage.style.getPropertyValue(property), priority = stage.style.getPropertyPriority(property);
  let limits = graphHeightLimits(window.innerWidth, window.innerHeight), desired: number | undefined;
  try {
    const saved = storageKey ? window.localStorage.getItem(storageKey) : null;
    if (saved && Number.isFinite(Number(saved)) && Number(saved) >= 200 && Number(saved) <= 10000) desired = Number(saved);
  } catch { /* Device-local preferences are optional; the canvas remains resizable. */ }
  let current = 0, pending = 0, frame: number | undefined, disposed = false;
  let drag: { id: number; y: number; height: number } | undefined;
  const paint = (value = desired ?? limits.defaultHeight) => {
    current = clampGraphHeight(value, limits);
    stage.style.setProperty(property, `${current}px`);
    handle.setAttribute("aria-valuenow", String(current));
    handle.setAttribute("aria-valuemin", String(limits.min));
    handle.setAttribute("aria-valuemax", String(limits.max));
    handle.setAttribute("aria-valuetext", `画布高度 ${current} 像素`);
  };
  const save = () => {
    desired = current;
    try { if (storageKey) window.localStorage.setItem(storageKey, String(current)); } catch { /* Keep in this mount. */ }
  };
  const cancelFrame = () => { if (frame !== undefined) window.cancelAnimationFrame(frame); frame = undefined; };
  const finish = (commit: boolean, y?: number) => {
    if (!drag) return;
    const previous = drag; drag = undefined; cancelFrame();
    if (commit) { paint(y === undefined ? pending : previous.height + y - previous.y); save(); }
    else paint();
    delete stage.dataset.heightResizing;
    try { if (handle.hasPointerCapture(previous.id)) handle.releasePointerCapture(previous.id); } catch { /* Detached node. */ }
  };
  const down = (event: PointerEvent) => {
    if (disposed || drag || event.button !== 0 || event.isPrimary === false) return;
    event.preventDefault(); event.stopPropagation(); handle.focus({ preventScroll: true });
    limits = graphHeightLimits(window.innerWidth, window.innerHeight);
    drag = { id: event.pointerId, y: event.clientY, height: current }; pending = current;
    try { handle.setPointerCapture(event.pointerId); } catch { drag = undefined; return; }
    stage.dataset.heightResizing = "true";
  };
  const move = (event: PointerEvent) => {
    if (!drag || drag.id !== event.pointerId) return;
    event.preventDefault(); event.stopPropagation();
    pending = drag.height + event.clientY - drag.y;
    if (frame === undefined) frame = window.requestAnimationFrame(() => {
      frame = undefined; if (!disposed && drag) paint(pending);
    });
  };
  const up = (event: PointerEvent) => {
    if (drag?.id !== event.pointerId) return;
    event.preventDefault(); event.stopPropagation(); finish(true, event.clientY);
  };
  const cancel = (event: PointerEvent) => { if (drag?.id === event.pointerId) finish(false); };
  const reset = (event: MouseEvent) => {
    event.preventDefault(); event.stopPropagation(); finish(false); desired = undefined; paint();
    try { if (storageKey) window.localStorage.removeItem(storageKey); } catch { /* Session-only default. */ }
  };
  const key = (event: KeyboardEvent) => {
    if (event.key === "Escape" && drag) { event.preventDefault(); event.stopPropagation(); finish(false); return; }
    const step = event.shiftKey ? 80 : 24;
    const value = event.key === "ArrowUp" ? current - step : event.key === "ArrowDown" ? current + step :
      event.key === "Home" ? limits.min : event.key === "End" ? limits.max : undefined;
    if (value !== undefined) { event.preventDefault(); event.stopPropagation(); finish(false); paint(value); save(); }
  };
  const resized = () => {
    if (disposed) return;
    finish(false); limits = graphHeightLimits(window.innerWidth, window.innerHeight); paint();
  };
  const blur = () => finish(false);
  const click = (event: MouseEvent) => { event.preventDefault(); event.stopPropagation(); };
  paint();
  handle.addEventListener("pointerdown", down);
  handle.addEventListener("pointermove", move);
  handle.addEventListener("pointerup", up);
  handle.addEventListener("pointercancel", cancel);
  handle.addEventListener("lostpointercapture", cancel);
  handle.addEventListener("dblclick", reset);
  handle.addEventListener("keydown", key);
  handle.addEventListener("click", click);
  window.addEventListener("resize", resized);
  window.addEventListener("blur", blur);
  return () => {
    if (disposed) return;
    disposed = true; finish(false); cancelFrame();
    handle.removeEventListener("pointerdown", down);
    handle.removeEventListener("pointermove", move);
    handle.removeEventListener("pointerup", up);
    handle.removeEventListener("pointercancel", cancel);
    handle.removeEventListener("lostpointercapture", cancel);
    handle.removeEventListener("dblclick", reset);
    handle.removeEventListener("keydown", key);
    handle.removeEventListener("click", click);
    window.removeEventListener("resize", resized);
    window.removeEventListener("blur", blur);
    delete stage.dataset.heightResizing;
    if (original) stage.style.setProperty(property, original, priority); else stage.style.removeProperty(property);
  };
}
