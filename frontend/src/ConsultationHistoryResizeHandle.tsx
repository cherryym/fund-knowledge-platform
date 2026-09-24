import { useLayoutEffect, useRef } from "react";
import { DotsSixVertical } from "@phosphor-icons/react";
import { attachPreviewResize } from "./previewResize";

export function ConsultationHistoryResizeHandle({ ownerId, spaceId, panelId }:
  { ownerId: string; spaceId: string; panelId: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const handle = ref.current, host = handle?.closest<HTMLElement>(".consultation-layout");
    if (!handle || !host) return;
    return attachPreviewResize(handle, host, "consultation-history",
      `fkb:consultation-history-width:v1:${ownerId}:${spaceId}`);
  }, [ownerId, spaceId]);
  return <div ref={ref} className="consultation-history-resize" role="separator" tabIndex={0}
    aria-label="调整咨询历史宽度" aria-orientation="vertical" aria-controls={panelId}
    aria-valuemin={160} aria-valuemax={480} aria-valuenow={200}
    title="左右拖动调整历史栏宽度；双击恢复默认；左右方向键微调">
    <DotsSixVertical aria-hidden="true" />
  </div>;
}
