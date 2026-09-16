/** Direct split resizing: one layout write per animation frame, never per mouse event. */
export type PreviewResizeMode = "dock" | "drawer" | "wiki-navigation" | "document-navigation";
export function previewResizeLimits(mode: PreviewResizeMode, available: number, hasPreview = false) {
  const width = Number.isFinite(available) ? Math.max(0, available) : 0;
  if (mode === "document-navigation") {
    // The dock also needs its own minimum (280px), in addition to the list (320px).
    const max = Math.max(0, Math.min(520, width - (hasPreview ? 600 : 480)));
    return { min: Math.min(184, max), max, defaultWidth: 184 };
  }
  if (mode === "wiki-navigation") {
    const max = Math.max(0, Math.min(560, width - 480));
    return { min: Math.min(188, max), max, defaultWidth: 188 };
  }
  const max = Math.max(0, mode === "dock" ? width - 320 : width - (width > 600 ? 24 : 0));
  return { min: Math.min(280, max), max, defaultWidth: mode === "dock" ? 320 : 500 };
}
export function clampPreviewWidth(value: number, limits: ReturnType<typeof previewResizeLimits>) {
  return Math.round(Math.min(limits.max, Math.max(limits.min, Number.isFinite(value) ? value : limits.defaultWidth)));
}

export function attachPreviewResize(handle: HTMLElement, host: HTMLElement, mode: PreviewResizeMode, storageKey: string) {
  const navigation = mode === "wiki-navigation" || mode === "document-navigation";
  const property = mode === "document-navigation" ? "--document-category-width" : mode === "wiki-navigation" ? "--wiki-navigation-width" : mode === "dock" ? "--document-preview-width" : "--preview-drawer-width";
  const direction = navigation ? 1 : -1;
  const availableWidth = () => mode === "drawer" ? window.innerWidth : host.clientWidth;
  const workspace = mode === "document-navigation" ? host.querySelector<HTMLElement>(":scope > .resource-workspace") : null;
  const previewVisible = () => Boolean(workspace && !workspace.classList.contains("no-overview") && workspace.querySelector(":scope > .document-inspector"));
  const oldValue = host.style.getPropertyValue(property), oldPriority = host.style.getPropertyPriority(property);
  let measuredWidth = availableWidth(), measuredPreview = previewVisible();
  let limits = previewResizeLimits(mode, measuredWidth, measuredPreview);
  let desired = limits.defaultWidth;
  try {
    const saved = window.localStorage.getItem(storageKey);
    if (saved && Number.isFinite(Number(saved)) && Number(saved) > 0 && Number(saved) < 10000) desired = Number(saved);
  } catch { /* Device-local preference is optional. */ }
  let current = clampPreviewWidth(desired, limits), pending = current;
  let frame: number | undefined, disposed = false, swallowClick = false;
  let drag: { pointerId: number; x: number; width: number } | undefined;
  const paint = (value: number) => {
    current = clampPreviewWidth(value, limits);
    host.style.setProperty(property, `${current}px`);
    handle.setAttribute("aria-valuenow", String(current));
    handle.setAttribute("aria-valuemin", String(limits.min));
    handle.setAttribute("aria-valuemax", String(limits.max));
    handle.setAttribute("aria-valuetext", `${current} 像素`);
    handle.setAttribute("aria-disabled", String(limits.max <= limits.min));
  };
  const save = () => {
    desired = current;
    try { window.localStorage.setItem(storageKey, String(current)); } catch { /* Resizing still works. */ }
  };
  const cancelFrame = () => { if (frame !== undefined) window.cancelAnimationFrame(frame); frame = undefined; };
  const schedule = (value: number) => {
    pending = value;
    if (frame === undefined) frame = window.requestAnimationFrame(() => {
      frame = undefined;
      if (!disposed) paint(pending);
    });
  };
  const finish = (commit: boolean, clientX?: number) => {
    if (!drag) return;
    const previous = drag;
    drag = undefined;
    cancelFrame();
    if (commit) { paint(clientX === undefined ? pending : previous.width + direction * (clientX - previous.x)); save(); }
    else paint(desired);
    delete host.dataset.previewResizing;
    try { if (handle.hasPointerCapture(previous.pointerId)) handle.releasePointerCapture(previous.pointerId); } catch { /* Detached surface. */ }
  };
  const down = (event: PointerEvent) => {
    if (disposed || drag || event.button !== 0 || event.isPrimary === false || limits.max <= limits.min) return;
    event.preventDefault(); event.stopPropagation(); handle.focus({preventScroll:true});
    // Read geometry only when the gesture starts, not during high-frequency moves.
    measuredWidth = availableWidth();
    measuredPreview = previewVisible();
    limits = previewResizeLimits(mode, measuredWidth, measuredPreview);
    drag = {pointerId:event.pointerId, x:event.clientX, width:current}; pending = current;
    try { handle.setPointerCapture(event.pointerId); } catch { drag = undefined; return; }
    swallowClick = true;
    host.dataset.previewResizing = "true";
  };
  const move = (event: PointerEvent) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    event.preventDefault(); schedule(drag.width + direction * (event.clientX - drag.x));
  };
  const up = (event: PointerEvent) => { if (drag?.pointerId === event.pointerId) finish(true, event.clientX); };
  const cancel = (event?: PointerEvent) => { if (!event || drag?.pointerId === event.pointerId) finish(false); };
  const reset = (event: MouseEvent) => { event.preventDefault(); finish(false); paint(limits.defaultWidth); save(); };
  const key = (event: KeyboardEvent) => {
    if (event.key === "Escape" && drag) { event.preventDefault(); event.stopPropagation(); finish(false); return; }
    const step = event.shiftKey ? 80 : 24;
    const value = event.key === "ArrowLeft" ? current - direction * step : event.key === "ArrowRight" ? current + direction * step :
      event.key === "Home" ? limits.min : event.key === "End" ? limits.max : undefined;
    if (value !== undefined) { event.preventDefault(); event.stopPropagation(); paint(value); save(); }
  };
  const resize = () => {
    if (disposed) return;
    const width = availableWidth(), hasPreview = previewVisible();
    // Reflowing category/reader text changes the auto-height during a drag.
    // Only a width change should cancel the gesture and recalculate its limits.
    if (navigation && width === measuredWidth && hasPreview === measuredPreview) return;
    finish(false);
    measuredWidth = width;
    measuredPreview = hasPreview;
    limits = previewResizeLimits(mode, width, hasPreview);
    paint(desired);
  };
  const blur = () => finish(false);
  // Capture can retarget the release click to the dialog/backdrop as its width
  // moves under the pointer. Consume that click, never close/select as a side effect.
  const nextPointer = () => { if (!drag) swallowClick = false; };
  const click = (event: MouseEvent) => {
    if (swallowClick) { swallowClick = false; event.preventDefault(); event.stopPropagation(); }
  };
  paint(desired);
  host.addEventListener("pointerdown", nextPointer, true);
  host.addEventListener("click", click, true);
  handle.addEventListener("pointerdown", down);
  handle.addEventListener("pointermove", move);
  handle.addEventListener("pointerup", up);
  handle.addEventListener("pointercancel", cancel);
  handle.addEventListener("lostpointercapture", cancel);
  handle.addEventListener("dblclick", reset);
  handle.addEventListener("keydown", key);
  window.addEventListener("resize", resize);
  window.addEventListener("blur", blur);
  const observer = typeof ResizeObserver === "undefined" || mode === "drawer" ? undefined : new ResizeObserver(resize);
  observer?.observe(host);
  // Opening/closing the preview does not change the outer width. Observe only
  // the sibling's class/children, never its style or document content subtree.
  const companionObserver = workspace && typeof window.MutationObserver !== "undefined" ? new window.MutationObserver(resize) : undefined;
  if (workspace) companionObserver?.observe(workspace, {attributes: true, attributeFilter: ["class"], childList: true});
  return () => {
    if (disposed) return;
    disposed = true; finish(false); cancelFrame(); observer?.disconnect(); companionObserver?.disconnect();
    host.removeEventListener("pointerdown", nextPointer, true);
    host.removeEventListener("click", click, true);
    handle.removeEventListener("pointerdown", down);
    handle.removeEventListener("pointermove", move);
    handle.removeEventListener("pointerup", up);
    handle.removeEventListener("pointercancel", cancel);
    handle.removeEventListener("lostpointercapture", cancel);
    handle.removeEventListener("dblclick", reset);
    handle.removeEventListener("keydown", key);
    window.removeEventListener("resize", resize);
    window.removeEventListener("blur", blur);
    delete host.dataset.previewResizing;
    if (oldValue) host.style.setProperty(property, oldValue, oldPriority); else host.style.removeProperty(property);
  };
}
