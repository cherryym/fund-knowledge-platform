import { useLayoutEffect, useRef, type RefObject } from "react";
import { attachGraphHeightResize } from "./graphStageResize";

export function GraphHeightResizeHandle({ stage, panelId, preferenceKey }: {
  stage: RefObject<HTMLDivElement | null>; panelId: string; preferenceKey?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (!ref.current || !stage.current) return;
    return attachGraphHeightResize(ref.current, stage.current, preferenceKey);
  }, [stage, preferenceKey]);
  return <div ref={ref} className="star-height-resize" role="separator" tabIndex={0}
    aria-label="调整图谱高度" aria-orientation="horizontal" aria-controls={panelId}
    title="上下拖动调整画布高度；双击恢复默认；上下方向键微调">
    <span className="star-height-grip" aria-hidden="true" /><span>拖动调整高度</span>
  </div>;
}
