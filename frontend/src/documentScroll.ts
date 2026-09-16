/** Native scrolling owns input; only the decorative progress is frame-batched. */
export function attachDocumentProgress(viewport: HTMLElement, indicator: HTMLElement, paper: HTMLElement | null) {
  let frame = 0;
  let disposed = false;
  const update = () => {
    frame = 0;
    if (disposed) return;
    const range = viewport.scrollHeight - viewport.clientHeight;
    const ratio = range > 0 ? Math.max(0, Math.min(1, viewport.scrollTop / range)) : 1;
    indicator.style.transform = `scaleX(${ratio})`;
  };
  const schedule = () => {
    if (!disposed && !frame) frame = requestAnimationFrame(update);
  };
  viewport.addEventListener("scroll", schedule, { passive: true });
  const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(schedule);
  resize?.observe(viewport);
  if (paper) resize?.observe(paper);
  schedule();
  return () => {
    disposed = true;
    viewport.removeEventListener("scroll", schedule);
    resize?.disconnect();
    if (frame) cancelAnimationFrame(frame);
  };
}
