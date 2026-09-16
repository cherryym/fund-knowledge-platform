import { useLayoutEffect, useRef } from "react";
import { DotsSixVertical } from "@phosphor-icons/react";
import { attachPreviewResize } from "./previewResize";

export function PreviewResizeHandle({ drawer, ownerId, panelId }: { drawer: boolean; ownerId: string; panelId: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const handle = ref.current;
    const host = handle?.closest<HTMLElement>(drawer ? ".document-preview-drawer" : ".resource-workspace");
    if (!handle || !host) return;
    return attachPreviewResize(handle, host, drawer ? "drawer" : "dock", `fkb:preview-width:v1:${ownerId}:${drawer ? "drawer" : "dock"}`);
  }, [drawer, ownerId]);
  return <div ref={ref} className="preview-resize-handle" role="separator" tabIndex={0} aria-label="调整文档预览宽度"
    aria-orientation="vertical" aria-controls={panelId} aria-valuemin={280} aria-valuemax={1480} aria-valuenow={drawer ? 500 : 320}
    title="拖动调整预览宽度；双击恢复默认；方向键微调"><DotsSixVertical aria-hidden="true" /></div>;
}
