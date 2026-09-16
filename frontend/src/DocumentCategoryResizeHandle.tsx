import { useLayoutEffect, useRef } from "react";
import { DotsSixVertical } from "@phosphor-icons/react";
import { attachPreviewResize } from "./previewResize";

export function DocumentCategoryResizeHandle({ ownerId, spaceId, panelId }:
  { ownerId: string; spaceId: string; panelId: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const handle = ref.current, host = handle?.closest<HTMLElement>(".document-layout");
    if (!handle || !host) return;
    return attachPreviewResize(handle, host, "document-navigation", `fkb:document-category-width:v1:${ownerId}:${spaceId}`);
  }, [ownerId, spaceId]);
  return <div ref={ref} className="document-category-resize" role="separator" tabIndex={0}
    aria-label="调整文档分类宽度" aria-orientation="vertical" aria-controls={panelId}
    aria-valuemin={184} aria-valuemax={520} aria-valuenow={184}
    title="向右拖动加宽分类栏；双击恢复默认；左右方向键微调">
    <DotsSixVertical aria-hidden="true"/>
  </div>;
}
